"""药剂单据导入与对账文件归档。

导入分两步：
1. preview：解析 CSV、逐条校验、按「导入后」口径预演结存，返回差异预览，不写数据；
2. commit：凭预览令牌复核（令牌只保留最近一次，防止过期/串批），原子替换整张药剂表，
   落库成功后才把原始导入文件与对账文件一起归档。任何一步失败都不会留下半张台账。
"""
from __future__ import annotations

import csv
import io
import json
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import settings
from app.services import chemical_ledger as ledger

# 导入模板列：前 8 列与台账列对齐，最后一列单独标识出入方向。
TEMPLATE_COLUMNS = ["单据编号", "药剂名称", "规格型号", "出入数量", "供应商", "经办人员", "出入方向"]
# CSV 里方向写法允许的别名，统一归到「入库/出库」。
DIRECTION_ALIASES = {
    "入库": ledger.INBOUND,
    "入": ledger.INBOUND,
    "in": ledger.INBOUND,
    "入库单": ledger.INBOUND,
    "出库": ledger.OUTBOUND,
    "出": ledger.OUTBOUND,
    "out": ledger.OUTBOUND,
    "出库单": ledger.OUTBOUND,
}
REQUIRED_IMPORT_FIELDS = ["单据编号", "药剂名称", "规格型号", "出入数量", "供应商"]
MAX_IMPORT_ROWS = 5000

# 归档目录：真实项目会换成对象存储；这里落到本地 var/archives 方便取文件。
ARCHIVE_ROOT = Path(getattr(settings, "archive_dir", "var/archives"))


class _PendingPreview:
    def __init__(self, *, parsed_rows: list[dict[str, Any]], digest: str, summary: dict[str, Any]):
        self.parsed_rows = parsed_rows
        self.digest = digest
        self.summary = summary
        self.created_at = datetime.now()


class ChemicalImportService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: _PendingPreview | None = None
        self._pending_token: str | None = None

    # ---------- 模板与错误清单 ----------

    def template_csv(self) -> str:
        """生成 UTF-8 带 BOM 的导入模板，Excel 直接打开不乱码。"""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(TEMPLATE_COLUMNS)
        writer.writerow(["CHEM-202609-0001", "聚合氯化铝", "25kg/袋", "100", "华星化工", "张三", "入库"])
        writer.writerow(["CHEM-202609-0002", "聚合氯化铝", "25kg/袋", "30", "华星化工", "张三", "出库"])
        return "﻿" + buffer.getvalue()

    def error_report_csv(self, errors: list[dict[str, Any]]) -> str:
        """把逐行校验错误整理成可下载的错误明细清单。"""
        buffer = io.StringIO()
        fieldnames = ["行号", "单据编号", "错误原因"]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        for item in errors:
            writer.writerow({key: item.get(key, "") for key in fieldnames})
        return "﻿" + buffer.getvalue()

    # ---------- 第一步：解析 + 校验 + 差异预览 ----------

    def preview(
        self,
        content: str,
        existing_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """解析导入文本，逐条拦截问题行，并对「假设全部落库后」的结存做差异预演。"""
        parsed_rows, errors = self._parse(content, existing_rows)
        digest = self._digest(parsed_rows, errors)

        before_closing = ledger.closing_balances(existing_rows)
        # 预演：复制一份台账加上导入行（导入单确认后即按「已出入库」参与结存），
        # 只在副本上重算，绝不触碰真实数据。
        simulated = [dict(row) for row in existing_rows]
        simulated_seq = 0
        for values in parsed_rows:
            simulated_row = dict(values)
            simulated_row["id"] = 10_000_000 + simulated_seq
            simulated_seq += 1
            simulated_row["status"] = ledger.POSTED_STATUS
            simulated_row["单据状态"] = ledger.POSTED_STATUS
            simulated.append(simulated_row)
        ledger.recompute_balances(simulated)
        after_closing = ledger.closing_balances(simulated)
        negatives = ledger.negative_balances(simulated)

        row_changes = self._row_changes(parsed_rows, simulated)
        balance_changes = self._balance_changes(before_closing, after_closing)

        summary = {
            "total_rows": len(parsed_rows) + len(errors),
            "valid_rows": len(parsed_rows),
            "error_rows": len(errors),
            "inbound_rows": sum(1 for row in parsed_rows if row["出入方向"] == ledger.INBOUND),
            "outbound_rows": sum(1 for row in parsed_rows if row["出入方向"] == ledger.OUTBOUND),
            "negative_balances": negatives,
        }
        can_commit = not errors
        with self._lock:
            token = secrets.token_urlsafe(16)
            self._pending_token = token
            self._pending = _PendingPreview(parsed_rows=parsed_rows, digest=digest, summary=summary)
        return {
            "token": token,
            "can_commit": can_commit,
            "errors": errors,
            "row_changes": row_changes,
            "balance_changes": balance_changes,
            "summary": summary,
        }

    def _parse(
        self, content: str, existing_rows: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        text = (content or "").lstrip("﻿").strip()
        if not text:
            return [], [{"行号": 1, "单据编号": "", "错误原因": "导入文件为空，请按模板填写后再上传"}]
        reader = csv.reader(io.StringIO(text))
        rows = [row for row in reader]
        if not rows:
            return [], [{"行号": 1, "单据编号": "", "错误原因": "导入文件为空，请按模板填写后再上传"}]

        header = [cell.strip() for cell in rows[0]]
        missing_columns = [name for name in REQUIRED_IMPORT_FIELDS if name not in header]
        if missing_columns:
            return [], [
                {"行号": 1, "单据编号": "", "错误原因": f"缺少模板必需列：{'、'.join(missing_columns)}，请下载最新模板"}
            ]
        index = {name: header.index(name) for name in header}

        parsed: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        seen_codes: set[str] = set()
        existing_codes = {str(row.get("单据编号") or "").strip() for row in existing_rows}
        data_rows = rows[1:]
        if len(data_rows) > MAX_IMPORT_ROWS:
            errors.append({
                "行号": 0,
                "单据编号": "",
                "错误原因": f"单次最多导入 {MAX_IMPORT_ROWS} 行，当前 {len(data_rows)} 行，请拆分后再导入",
            })
            data_rows = data_rows[:MAX_IMPORT_ROWS]

        for offset, cells in enumerate(data_rows, start=2):
            pick = lambda name: cells[index[name]].strip() if name in index and index[name] < len(cells) else ""
            code = pick("单据编号")
            row_errors: list[str] = []

            values = {name: pick(name) for name in TEMPLATE_COLUMNS if name != "出入方向"}
            direction_raw = pick("出入方向") or ledger.INBOUND
            direction = DIRECTION_ALIASES.get(direction_raw.lower() if direction_raw.isascii() else direction_raw)
            if direction is None:
                row_errors.append(f"出入方向「{direction_raw}」无法识别，只允许入库或出库")
                direction = ledger.INBOUND

            for field in REQUIRED_IMPORT_FIELDS:
                if not pick(field):
                    row_errors.append(f"缺少必填字段「{field}」")

            quantity_text = pick("出入数量")
            quantity: Any = None
            if quantity_text:
                try:
                    decimal_quantity = ledger.Decimal(quantity_text.replace(",", "").strip())
                    invalid_number = False
                except ledger.InvalidOperation:
                    decimal_quantity = ledger.Decimal("0")
                    invalid_number = True
                if invalid_number or not decimal_quantity.is_finite():
                    row_errors.append(f"出入数量「{quantity_text}」不是有效数字")
                elif decimal_quantity < 0:
                    row_errors.append(f"出入数量不能为负（当前 {quantity_text}），出库请把出入方向填为出库")
                elif decimal_quantity == 0:
                    row_errors.append("出入数量不能为 0")
                else:
                    quantity = int(decimal_quantity) if decimal_quantity == decimal_quantity.to_integral_value() else float(decimal_quantity)

            if not code:
                code = ""
            elif code in existing_codes:
                row_errors.append(f"单据编号「{code}」与库内既有单据重复，不能重复导入")
            elif code in seen_codes:
                row_errors.append(f"单据编号「{code}」在本次导入文件中重复出现")

            if row_errors:
                errors.append({"行号": offset, "单据编号": code, "错误原因": "；".join(row_errors)})
                continue
            seen_codes.add(code)
            values["出入数量"] = quantity
            values["出入方向"] = direction
            parsed.append(values)

        if not data_rows:
            errors.append({"行号": 0, "单据编号": "", "错误原因": "文件只有表头没有数据行，请填写单据后再导入"})
        return parsed, errors

    def _row_changes(self, parsed_rows: list[dict[str, Any]], simulated: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """本次每张导入单落库后的流水结存预览。"""
        code_index = {str(row.get("单据编号")): row for row in simulated}
        changes = []
        for values in parsed_rows:
            row = code_index[str(values["单据编号"])]
            changes.append({
                "单据编号": values["单据编号"],
                "药剂名称": values["药剂名称"],
                "规格型号": values["规格型号"],
                "出入方向": values["出入方向"],
                "出入数量": values["出入数量"],
                "结存数量": row["结存数量"],
                "供应商": values["供应商"],
                "经办人员": values.get("经办人员", ""),
            })
        return changes

    def _balance_changes(
        self,
        before: dict[tuple[str, str], Any],
        after: dict[tuple[str, str], Any],
    ) -> list[dict[str, Any]]:
        """按药剂汇总导入前后的结存差异。"""
        changes = []
        for key in sorted(set(before) | set(after)):
            old = before.get(key)
            new = after.get(key)
            if old == new:
                continue
            changes.append({
                "药剂名称": key[0],
                "规格型号": key[1],
                "导入前结存": ledger.format_number(old) if old is not None else "0",
                "导入后结存": ledger.format_number(new) if new is not None else "0",
                "差异": ledger.format_number((new or ledger.Decimal("0")) - (old or ledger.Decimal("0"))),
            })
        return changes

    def _digest(self, parsed_rows: list[dict[str, Any]], errors: list[dict[str, Any]]) -> str:
        payload = json.dumps(
            {"rows": parsed_rows, "errors": errors}, ensure_ascii=False, sort_keys=True, default=str
        )
        return f"{len(payload):x}-{uuid.uuid5(uuid.NAMESPACE_OID, payload).hex[:12]}"

    # ---------- 第二步：复核 + 原子落库 + 归档 ----------

    def commit(
        self,
        token: str,
        existing_rows: list[dict[str, Any]],
        replace_rows: Any,
        raw_content: str = "",
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        """凭预览令牌落库。返回 (结果, 错误说明)；失败时台账保持原样。"""
        with self._lock:
            if not token or token != self._pending_token or self._pending is None:
                return None, "预览已失效或不存在，请重新上传文件生成差异预览"
            pending = self._pending
            if pending.summary["error_rows"]:
                return None, "仍存在校验不通过的明细，请修正后重新预览再确认"
            parsed_rows = [dict(row) for row in pending.parsed_rows]

        # 提交前再查一次重复单据：预览到确认之间库内若新增了同号单据，整批拦下。
        current_codes = {str(row.get("单据编号") or "").strip() for row in existing_rows}
        conflicts = sorted({row["单据编号"] for row in parsed_rows if row["单据编号"] in current_codes})
        if conflicts:
            return None, f"以下单据编号在确认前已被占用：{'、'.join(conflicts)}，请重新预览"

        next_id = max((int(row.get("id", 0)) for row in existing_rows), default=0) + 1
        new_rows: list[dict[str, Any]] = []
        for values in parsed_rows:
            entry: dict[str, Any] = {"id": next_id}
            next_id += 1
            entry.update(values)
            entry["status"] = ledger.POSTED_STATUS
            entry["单据状态"] = ledger.POSTED_STATUS
            entry["pending"] = False
            entry["abnormal"] = False
            new_rows.append(entry)

        combined = [dict(row) for row in existing_rows]
        combined.extend(new_rows)
        ledger.recompute_balances(combined)

        # 落库前自检：重算结果必须与流水对得上，对不上整批回滚，不允许写一半。
        mismatches = ledger.verify_ledger(combined)
        if mismatches:
            return None, f"结存重算自检未通过（{len(mismatches)} 项不一致），本次导入已全部回滚"

        # 先准备归档内容，全部生成成功后再切换台账，避免「表已改、归档失败」的半成品。
        archive_id, files = self._build_archive(
            parsed_rows=parsed_rows,
            combined=combined,
            raw_content=raw_content,
            operator=operator,
            summary=pending.summary,
        )
        replace_rows(combined)

        with self._lock:
            self._pending = None
            self._pending_token = None

        return {
            "message": f"已导入 {len(new_rows)} 张单据并完成结存重算，对账文件已归档（归档号 {archive_id}）",
            "imported": len(new_rows),
            "archive_id": archive_id,
            "files": files,
            "summary": pending.summary,
        }, ""

    def _build_archive(
        self,
        *,
        parsed_rows: list[dict[str, Any]],
        combined: list[dict[str, Any]],
        raw_content: str,
        operator: str,
        summary: dict[str, Any],
    ) -> tuple[str, list[str]]:
        archive_id = datetime.now().strftime("ARC-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        directory = ARCHIVE_ROOT / archive_id
        directory.mkdir(parents=True, exist_ok=False)

        import_name = "import_source.csv"
        (directory / import_name).write_text(raw_content or "", encoding="utf-8")

        reconciliation = self._build_reconciliation(parsed_rows, combined, summary)
        recon_name = "reconciliation.json"
        (directory / recon_name).write_text(
            json.dumps(reconciliation, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

        detail_name = "reconciliation_detail.csv"
        (directory / detail_name).write_text(self._build_reconciliation_csv(combined), encoding="utf-8")

        manifest = {
            "archive_id": archive_id,
            "archived_at": datetime.now().isoformat(timespec="seconds"),
            "operator": operator or "系统",
            "imported": len(parsed_rows),
            "files": [import_name, recon_name, detail_name],
            "summary": summary,
        }
        (directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return archive_id, [import_name, recon_name, detail_name, "manifest.json"]

    def _build_reconciliation(
        self, parsed_rows: list[dict[str, Any]], combined: list[dict[str, Any]], summary: dict[str, Any]
    ) -> dict[str, Any]:
        imported_codes = {row["单据编号"] for row in parsed_rows}
        imported_rows = [row for row in combined if str(row.get("单据编号")) in imported_codes]
        closing = ledger.closing_balances(combined)
        return {
            "archive_type": "chemical_import_reconciliation",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "import_summary": summary,
            "imported_documents": [
                {
                    "单据编号": row.get("单据编号"),
                    "药剂名称": row.get("药剂名称"),
                    "规格型号": row.get("规格型号"),
                    "出入方向": ledger.direction_of(row),
                    "出入数量": row.get("出入数量"),
                    "单据结存": row.get("结存数量"),
                    "供应商": row.get("供应商"),
                }
                for row in sorted(imported_rows, key=lambda item: str(item.get("单据编号")))
            ],
            "closing_balances": [
                {
                    "药剂名称": key[0],
                    "规格型号": key[1],
                    "期末结存": ledger.format_number(value),
                }
                for key, value in sorted(closing.items())
            ],
            "consistency_check": {
                "result": "通过" if not ledger.verify_ledger(combined) else "不通过",
                "mismatches": ledger.verify_ledger(combined),
            },
        }

    def _build_reconciliation_csv(self, combined: list[dict[str, Any]]) -> str:
        buffer = io.StringIO()
        columns = ["单据编号", "药剂名称", "规格型号", "出入方向", "出入数量", "结存数量", "供应商", "经办人员", "单据状态"]
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in sorted(combined, key=lambda item: (str(item.get("药剂名称")), str(item.get("单据编号")))):
            writer.writerow({
                "单据编号": row.get("单据编号", ""),
                "药剂名称": row.get("药剂名称", ""),
                "规格型号": row.get("规格型号", ""),
                "出入方向": ledger.direction_of(row),
                "出入数量": row.get("出入数量", ""),
                "结存数量": row.get("结存数量", ""),
                "供应商": row.get("供应商", ""),
                "经办人员": row.get("经办人员", ""),
                "单据状态": row.get("单据状态") or row.get("status", ""),
            })
        return "﻿" + buffer.getvalue()

    # ---------- 归档查询 ----------

    def list_archives(self) -> list[dict[str, Any]]:
        """扫描归档目录，列出每批导入的对账归档。"""
        if not ARCHIVE_ROOT.exists():
            return []
        items = []
        for directory in sorted(ARCHIVE_ROOT.iterdir(), reverse=True):
            manifest_path = directory / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            items.append(manifest)
        return items

    def archive_file_path(self, archive_id: str, filename: str) -> Path | None:
        """安全定位归档文件：只允许读取归档目录内的文件，不接受路径穿越。"""
        root = ARCHIVE_ROOT.resolve()
        target = (root / archive_id / filename).resolve()
        if root not in target.parents or not target.is_file():
            return None
        return target


import_service = ChemicalImportService()
