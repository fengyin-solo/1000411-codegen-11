"""药剂单据导入与结存重算业务规则。

流程分两段，中间不碰业务台账：
1. preview：解析 CSV、逐行校验（重复单据 / 数量为负 / 供应商缺失等），并模拟落库后的
   结存滚动，给出「重算前 → 重算后」差异预览。结果以批次号暂存到磁盘，不写台账。
2. commit：用户确认后才把新单据追加进台账并回写结存。提交采用「先算后写 + 快照回滚」：
   任何一步失败都会恢复到提交前，台账里不会留下只算了一半的结存。
"""
from __future__ import annotations

import copy
import csv
import io
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.services.chemical import MODULE, POSTED_STATUS
from app.services.chemical_import_store import ImportArchiveStore
from app.store import store

# 导入模板列：发生日期用于同一药剂的出入排序，经办人员可空（空时记为「导入」）
IMPORT_COLUMNS = ["单据编号", "药剂名称", "规格型号", "出入方向", "出入数量", "供应商", "经办人员", "发生日期"]
REQUIRED_COLUMNS = ["单据编号", "药剂名称", "规格型号", "出入方向", "出入数量", "供应商"]
DIRECTION_IN = "入库"
DIRECTION_OUT = "出库"
DIRECTIONS = (DIRECTION_IN, DIRECTION_OUT)


class ImportRejected(Exception):
    """导入被业务规则拒绝（文件无法解析或批次状态不对），属于 4xx 类错误。"""


class ConsistencyError(Exception):
    """结存重算后的自校验不通过，触发整批回滚。"""


@dataclass
class CommitPlan:
    """一份尚未应用的落库计划：所有目标值都在这里算好，应用阶段只做赋值。"""

    new_rows: list[dict[str, Any]]
    updates: list[tuple[dict[str, Any], int | float]]
    affected_keys: set[tuple[str, str]]
    in_total: int | float
    out_total: int | float
    warnings: list[str]
    affected_entries: list[dict[str, Any]]
    balances: dict[tuple[str, str], dict[str, Any]]


def _format_qty(value: float) -> int | float:
    """数量统一成数字：整数不带小数点，非整数保留 4 位去掉多余的尾零。"""
    if float(value).is_integer():
        return int(value)
    return round(value, 4)


def parse_csv_content(content: str) -> tuple[list[str], list[tuple[int, dict[str, str]]]]:
    """解析导入文件：返回表头与 (文件行号, 行数据)；空行跳过，BOM 自动去掉。"""
    text = (content or "").lstrip("﻿")
    if not text.strip():
        raise ImportRejected("导入文件为空，请先下载模板填写后再上传")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ImportRejected("导入文件缺少表头，请使用最新模板") from exc
    headers = [cell.strip() for cell in header]
    rows: list[tuple[int, dict[str, str]]] = []
    for line_no, raw in enumerate(reader, start=2):
        if not any(str(cell).strip() for cell in raw):
            continue
        values = {headers[index]: raw[index].strip() if index < len(raw) else "" for index in range(len(headers))}
        rows.append((line_no, values))
    if not rows:
        raise ImportRejected("导入文件没有可解析的数据行")
    return headers, rows


def _chemical_key(values: dict[str, Any]) -> tuple[str, str]:
    return str(values.get("药剂名称", "")).strip(), str(values.get("规格型号", "")).strip()


def _direction_of(row: dict[str, Any]) -> str:
    # 历史单据没有出入方向列，按入库口径参与重算，避免把期初库存算成负的
    return str(row.get("出入方向") or DIRECTION_IN).strip() or DIRECTION_IN


def _signed_qty(row: dict[str, Any]) -> float:
    quantity = float(row.get("出入数量", 0))
    return quantity if _direction_of(row) == DIRECTION_IN else -quantity


class ChemicalImportService:
    def __init__(self, archives: ImportArchiveStore | None = None) -> None:
        self.archives = archives or ImportArchiveStore()

    # ---- 模板与错误明细 ---------------------------------------------------

    def template_csv(self) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(IMPORT_COLUMNS)
        writer.writerow(["CHEM-IN-0001", "聚合氯化铝", "25kg/袋", "入库", 100, "华东药剂供应站", "张三", "2026-09-20"])
        writer.writerow(["CHEM-OUT-0001", "聚合氯化铝", "25kg/袋", "出库", 20, "华东药剂供应站", "李四", "2026-09-21"])
        return buffer.getvalue()

    def errors_csv(self, batch: dict[str, Any]) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["文件行号", "单据编号", "错误字段", "错误原因"])
        for error in batch.get("errors", []):
            writer.writerow([error["line"], error.get("code", ""), error.get("field", ""), error["reason"]])
        return buffer.getvalue()

    # ---- 第一段：解析、逐行校验、差异预览 ----------------------------------

    def preview(self, *, filename: str, content: str) -> dict[str, Any]:
        headers, rows = parse_csv_content(content)
        missing_columns = [column for column in REQUIRED_COLUMNS if column not in headers]
        if missing_columns:
            raise ImportRejected(
                f"导入文件缺少必需列：{'、'.join(missing_columns)}，请下载最新模板按列填写"
            )

        errors: list[dict[str, Any]] = []
        existing_rows = store.rows(MODULE)
        existing_codes = {str(row.get("单据编号", "")).strip(): row for row in existing_rows}

        # 文件内单据编号去重：先统计出现位置，第二行起逐行报重复原因
        seen_positions: dict[str, int] = {}

        def add_error(line_no: int, code: str, field: str, reason: str) -> None:
            errors.append({"line": line_no, "code": code, "field": field, "reason": reason})

        valid_rows: list[dict[str, Any]] = []
        for line_no, values in rows:
            code = values.get("单据编号", "").strip()
            row_errors = 0

            for field in REQUIRED_COLUMNS:
                if not values.get(field, "").strip():
                    reason = "供应商缺失，请补充供货单位后再导入" if field == "供应商" else f"{field}不能为空"
                    add_error(line_no, code, field, reason)
                    row_errors += 1

            direction = values.get("出入方向", "").strip()
            if direction and direction not in DIRECTIONS:
                add_error(line_no, code, "出入方向", f"出入方向「{direction}」无效，只能填「入库」或「出库」")
                row_errors += 1

            quantity_text = values.get("出入数量", "").strip()
            quantity: float | None = None
            if quantity_text:
                try:
                    quantity = float(quantity_text)
                except ValueError:
                    add_error(line_no, code, "出入数量", f"出入数量「{quantity_text}」不是有效数字")
                    row_errors += 1
                else:
                    if not math.isfinite(quantity):
                        add_error(line_no, code, "出入数量", f"出入数量「{quantity_text}」不是有效数字")
                        row_errors += 1
                    elif quantity < 0:
                        add_error(line_no, code, "出入数量", f"出入数量为负（{_format_qty(quantity)}），不允许导入")
                        row_errors += 1
                    elif quantity == 0:
                        add_error(line_no, code, "出入数量", "出入数量不能为 0")
                        row_errors += 1

            if code:
                if code in seen_positions:
                    add_error(
                        line_no, code, "单据编号",
                        f"单据编号「{code}」在导入文件中重复（首次出现在第 {seen_positions[code]} 行）",
                    )
                    row_errors += 1
                else:
                    seen_positions[code] = line_no
                duplicate = existing_codes.get(code)
                if duplicate is not None:
                    add_error(
                        line_no, code, "单据编号",
                        f"单据编号「{code}」已存在于药剂出入台账（记录 {duplicate.get('id')}，"
                        f"状态：{duplicate.get('status')}），不允许重复导入",
                    )
                    row_errors += 1

            if row_errors == 0 and quantity is not None:
                valid_rows.append({
                    "line": line_no,
                    "单据编号": code,
                    "药剂名称": values["药剂名称"].strip(),
                    "规格型号": values["规格型号"].strip(),
                    "出入方向": direction,
                    "出入数量": _format_qty(quantity),
                    "供应商": values["供应商"].strip(),
                    "经办人员": values.get("经办人员", "").strip() or "导入",
                    "发生日期": values.get("发生日期", "").strip(),
                })

        batch_id = self.archives.new_batch_id()
        batch: dict[str, Any] = {
            "batch_id": batch_id,
            "source_filename": filename or "药剂单据导入.csv",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "preview",
            "total_rows": len(rows),
            "errors": errors,
            "valid_rows": valid_rows,
            "raw_content": content,
        }

        # 即使存在错误也暂存批次：用户要能下载错误明细清单，逐行改完再重传
        self.archives.save_batch(batch)

        if errors:
            return {
                "batch_id": batch_id,
                "source_filename": batch["source_filename"],
                "created_at": batch["created_at"],
                "status": "preview",
                "valid": False,
                "total_rows": len(rows),
                "accepted_rows": 0,
                "error_count": len(errors),
                "errors": errors,
                "warnings": [],
                "summary": {},
                "balance_changes": [],
                "entries": [],
                "affected_entries": [],
            }

        plan = self._build_plan(valid_rows)
        batch["status"] = "ready"
        self.archives.save_batch(batch)
        return self._preview_response(batch, plan)

    def get_preview(self, batch_id: str) -> dict[str, Any]:
        """重新进入页面时按批次号找回差异预览，不要求用户重新上传。"""
        batch = self.archives.load_batch(batch_id)
        if batch is None:
            raise ImportRejected(f"导入批次 {batch_id} 不存在或已清理，请重新上传文件")
        if batch["status"] == "committed":
            result = batch.get("commit_result", {})
            return {"batch_id": batch_id, "status": "committed", "valid": True, **result}
        if batch.get("errors"):
            return {
                "batch_id": batch_id,
                "source_filename": batch.get("source_filename"),
                "created_at": batch.get("created_at"),
                "status": "preview",
                "valid": False,
                "total_rows": batch.get("total_rows"),
                "accepted_rows": 0,
                "error_count": len(batch["errors"]),
                "errors": batch["errors"],
                "warnings": [],
                "summary": {},
                "balance_changes": [],
                "entries": [],
                "affected_entries": [],
            }
        plan = self._build_plan(batch["valid_rows"])
        return self._preview_response(batch, plan)

    # ---- 结存重算（纯计算，不改台账） --------------------------------------

    @staticmethod
    def _row_date(row: dict[str, Any]) -> str:
        # 历史单据没有发生日期时排到最前，视为期初单据
        return str(row.get("发生日期", "")).strip()

    def _ordered_posted_rows(
        self, existing: list[dict[str, Any]], new_rows: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """已出入库单据按「发生日期 → 台账既有在前 → 台账 id/文件行号」滚动。

        补录早期单据时，后续所有单据的结存都会被重算，这才是完整的结存重算口径。
        """
        ordered: list[tuple[str, int, int, dict[str, Any]]] = []
        for index, row in enumerate(existing):
            if row.get("status") == POSTED_STATUS:
                ordered.append((self._row_date(row), 0, index, row))
        for index, row in enumerate(new_rows):
            ordered.append((self._row_date(row), 1, index, row))
        ordered.sort(key=lambda item: (item[0], item[1], item[2]))
        return [row for _, _, _, row in ordered]

    def _build_plan(self, valid_rows: list[dict[str, Any]]) -> CommitPlan:
        """根据当前台账与待导入行，模拟完整重算过程，产出可应用的落库计划。"""
        next_id = max((int(row.get("id", 0)) for row in store.rows(MODULE)), default=0) + 1
        new_rows: list[dict[str, Any]] = []
        for offset, values in enumerate(valid_rows):
            row = {
                "id": next_id + offset,
                "status": POSTED_STATUS,
                "pending": False,
                "abnormal": False,
                "来源": "单据导入",
                "文件行号": values["line"],
                "单据编号": values["单据编号"],
                "药剂名称": values["药剂名称"],
                "规格型号": values["规格型号"],
                "出入方向": values["出入方向"],
                "出入数量": values["出入数量"],
                "结存数量": None,
                "供应商": values["供应商"],
                "经办人员": values["经办人员"],
                "发生日期": values["发生日期"],
            }
            new_rows.append(row)

        existing = store.rows(MODULE)
        affected_keys = {_chemical_key(values) for values in valid_rows}

        # 重算前结存：只按当前台账已出入库单据滚动一遍
        before_totals = self._running_totals(
            [row for row in existing if row.get("status") == POSTED_STATUS]
        )

        updates: list[tuple[dict[str, Any], int | float]] = []
        running: dict[tuple[str, str], float] = {}
        warnings: list[str] = []
        for row in self._ordered_posted_rows(existing, new_rows):
            key = _chemical_key(row)
            if key not in affected_keys:
                continue
            balance = running.get(key, 0.0) + _signed_qty(row)
            running[key] = balance
            updates.append((row, _format_qty(balance)))
            if balance < 0:
                warnings.append(
                    f"药剂「{key[0]} / {key[1]}」在单据 {row.get('单据编号')} 后结存为 "
                    f"{_format_qty(balance)}，出库数量已超过库存"
                )

        in_total = _format_qty(sum(float(v["出入数量"]) for v in valid_rows if v["出入方向"] == DIRECTION_IN))
        out_total = _format_qty(sum(float(v["出入数量"]) for v in valid_rows if v["出入方向"] == DIRECTION_OUT))

        existing_ids = {id(row) for row in existing}
        new_id_set = {id(row) for row in new_rows}
        affected_entries: list[dict[str, Any]] = []
        balances: dict[tuple[str, str], dict[str, Any]] = {}
        for row, after in updates:
            key = _chemical_key(row)
            info = balances.setdefault(key, {
                "药剂名称": key[0], "规格型号": key[1],
                "期初结存": _format_qty(before_totals.get(key, 0.0)),
                "本次入库": 0.0, "本次出库": 0.0,
                "期末结存": after, "影响既有单据数": 0, "新增单据数": 0,
            })
            info["期末结存"] = after
            if id(row) in new_id_set:
                info["新增单据数"] += 1
                # 本次入/出库合计只统计导入新单；既有单据已体现在期初结存里
                if _direction_of(row) == DIRECTION_IN:
                    info["本次入库"] += abs(_signed_qty(row))
                else:
                    info["本次出库"] += abs(_signed_qty(row))
            elif id(row) in existing_ids:
                info["影响既有单据数"] += 1
                affected_entries.append({
                    "id": row.get("id"),
                    "单据编号": row.get("单据编号"),
                    "重算前结存": row.get("结存数量"),
                    "重算后结存": after,
                })
        for info in balances.values():
            info["本次入库"] = _format_qty(info["本次入库"])
            info["本次出库"] = _format_qty(info["本次出库"])

        return CommitPlan(
            new_rows=new_rows,
            updates=updates,
            affected_keys=affected_keys,
            in_total=in_total,
            out_total=out_total,
            warnings=warnings,
            affected_entries=affected_entries,
            balances=balances,
        )

    @staticmethod
    def _running_totals(posted_rows: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
        totals: dict[tuple[str, str], float] = {}
        for row in posted_rows:
            key = _chemical_key(row)
            totals[key] = totals.get(key, 0.0) + _signed_qty(row)
        return totals

    def _preview_response(self, batch: dict[str, Any], plan: CommitPlan) -> dict[str, Any]:
        new_balance = {id(row): after for row, after in plan.updates}
        entries = []
        for row in plan.new_rows:
            entries.append({
                "id": None,
                "文件行号": next((v["line"] for v in batch["valid_rows"] if v["单据编号"] == row["单据编号"]), None),
                "单据编号": row["单据编号"],
                "药剂名称": row["药剂名称"],
                "规格型号": row["规格型号"],
                "出入方向": row["出入方向"],
                "出入数量": row["出入数量"],
                "结存数量": new_balance.get(id(row)),
                "供应商": row["供应商"],
                "经办人员": row["经办人员"],
                "发生日期": row["发生日期"],
            })
        return {
            "batch_id": batch["batch_id"],
            "source_filename": batch["source_filename"],
            "created_at": batch["created_at"],
            "status": "ready",
            "valid": True,
            "total_rows": batch["total_rows"],
            "accepted_rows": len(plan.new_rows),
            "error_count": 0,
            "errors": [],
            "warnings": plan.warnings,
            "summary": {
                "新增单据数": len(plan.new_rows),
                "入库合计": plan.in_total,
                "出库合计": plan.out_total,
                "涉及药剂数": len(plan.affected_keys),
            },
            "balance_changes": list(plan.balances.values()),
            "entries": entries,
            "affected_entries": plan.affected_entries,
        }

    # ---- 第二段：确认落库（原子提交） --------------------------------------

    def commit(self, batch_id: str) -> dict[str, Any]:
        batch = self.archives.load_batch(batch_id)
        if batch is None:
            raise ImportRejected(f"导入批次 {batch_id} 不存在或已清理，请重新上传文件")
        if batch["status"] == "committed":
            return batch["commit_result"]
        if batch.get("errors"):
            raise ImportRejected("批次仍存在校验错误，请修正后重新上传再确认")
        if not batch.get("valid_rows"):
            raise ImportRejected("批次没有可导入的有效数据行")

        # 1) 先在台账快照之外把所有目标值算好，此阶段台账一行都不改
        rows = store.rows(MODULE)
        snapshot = copy.deepcopy(rows)
        original_ids = {int(row.get("id", 0)) for row in rows}
        plan = self._build_plan(batch["valid_rows"])
        preview_payload = self._preview_response(batch, plan)

        # 2) 先把对账文件与原始文件归档落盘；磁盘失败时台账还没动过
        files = self._reconciliation_files(batch, plan)
        try:
            archive_id = self.archives.archive_batch(
                batch=batch,
                files=files,
                result={
                    "total_rows": batch["total_rows"],
                    "accepted_rows": len(plan.new_rows),
                    "in_total": plan.in_total,
                    "out_total": plan.out_total,
                    "affected_chemicals": len(plan.affected_keys),
                },
            )
        except OSError as exc:
            raise ImportRejected(f"对账文件归档失败，本次导入未执行：{exc}") from exc

        # 3) 应用计划并立刻做结存=明细的自校验；不通过就整批回滚
        try:
            for row, balance in plan.updates:
                row["结存数量"] = balance
            # 行号只是计划期的辅助字段，不进入业务台账
            for row in plan.new_rows:
                row.pop("文件行号", None)
            rows.extend(plan.new_rows)
            self._assert_balances_reconcile(rows)
        except Exception as exc:
            # 原地回滚：把既有行恢复到快照值，并移除本次追加的新行，
            # 保证台账里不会留下只算了一半的结存
            restored = {int(item.get("id", 0)): item for item in snapshot}
            for row in list(rows):
                row_id = int(row.get("id", 0))
                if row_id in original_ids and row_id in restored:
                    row.clear()
                    row.update(restored[row_id])
                else:
                    rows.remove(row)
            self._remove_archive(archive_id)
            if isinstance(exc, ConsistencyError):
                raise ImportRejected(f"结存重算自校验未通过，已整批回滚：{exc}") from exc
            raise ImportRejected(f"落库过程出现异常，已整批回滚：{exc}") from exc

        result = {
            "archive_id": archive_id,
            "message": f"已导入 {len(plan.new_rows)} 条单据并重算结存，对账文件已归档（{archive_id}）",
            "total_rows": batch["total_rows"],
            "accepted_rows": len(plan.new_rows),
            "in_total": plan.in_total,
            "out_total": plan.out_total,
            "affected_chemicals": len(plan.affected_keys),
            "warnings": plan.warnings,
            "balance_changes": list(plan.balances.values()),
            "entries": preview_payload["entries"],
        }
        batch["status"] = "committed"
        batch["committed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        batch["archive_id"] = archive_id
        batch["commit_result"] = result
        # 已确认的批次保留归档目录；暂存目录同步更新状态，重新进入能看到已落库结果
        self.archives.save_batch({k: v for k, v in batch.items() if k != "raw_content"})
        return result

    @staticmethod
    def _assert_balances_reconcile(all_rows: list[dict[str, Any]]) -> None:
        """结存与明细对账：每种药剂按发生日期滚动到每张已出入库单据时的累计值，
        必须与该单上记的结存一致；不参与重算的药剂（未发生导入）不检查。"""
        posted = [row for row in all_rows if row.get("status") == POSTED_STATUS]
        imported_keys = {_chemical_key(row) for row in posted if row.get("来源") == "单据导入"}
        running: dict[tuple[str, str], float] = {}
        for row in sorted(posted, key=lambda row: (ChemicalImportService._row_date(row), int(row.get("id", 0)))):
            key = _chemical_key(row)
            if key not in imported_keys:
                continue
            balance = running.get(key, 0.0) + _signed_qty(row)
            running[key] = balance
            recorded = row.get("结存数量")
            if recorded is None or abs(float(recorded) - balance) > 1e-6:
                raise ConsistencyError(
                    f"单据 {row.get('单据编号')} 结存 {recorded} 与明细累计 {_format_qty(balance)} 不一致"
                )

    # ---- 对账文件 ---------------------------------------------------------

    def _reconciliation_files(self, batch: dict[str, Any], plan: CommitPlan) -> dict[str, str]:
        stamp = datetime.now().strftime("%Y%m%d")
        # 文件 1：原始导入文件原样归档
        source_name = f"导入原始文件_{stamp}.csv"
        # 文件 2：对账明细（受影响药剂的全部已出入库单据，按滚动顺序）
        detail_buffer = io.StringIO()
        writer = csv.writer(detail_buffer)
        writer.writerow([
            "序号", "单据编号", "药剂名称", "规格型号", "出入方向", "出入数量",
            "重算后结存", "供应商", "经办人员", "发生日期", "单据来源",
        ])
        existing = store.rows(MODULE)
        new_ids = {row["id"] for row in plan.new_rows}
        serial = 1
        for row, balance in plan.updates:
            writer.writerow([
                serial,
                row.get("单据编号", ""),
                row.get("药剂名称", ""),
                row.get("规格型号", ""),
                _direction_of(row),
                _format_qty(abs(float(row.get("出入数量", 0)))),
                balance,
                row.get("供应商", ""),
                row.get("经办人员", ""),
                row.get("发生日期", ""),
                "本次导入" if row.get("id") in new_ids else "台账既有",
            ])
            serial += 1

        # 文件 3：对账汇总（期初 + 入 - 出 = 期末，逐药剂给校验结论）
        summary_buffer = io.StringIO()
        summary_writer = csv.writer(summary_buffer)
        summary_writer.writerow([
            "药剂名称", "规格型号", "期初结存", "本次入库", "本次出库",
            "明细累计结存", "期末结存", "影响既有单据数", "新增单据数", "对账结论",
        ])
        for info in plan.balances.values():
            calculated = _format_qty(
                float(info["期初结存"]) + float(info["本次入库"]) - float(info["本次出库"])
            )
            reconciled = abs(float(calculated) - float(info["期末结存"])) <= 1e-6
            summary_writer.writerow([
                info["药剂名称"], info["规格型号"], info["期初结存"], info["本次入库"],
                info["本次出库"], calculated, info["期末结存"],
                info["影响既有单据数"], info["新增单据数"],
                "结存平衡" if reconciled else "结存不平衡",
            ])

        return {
            source_name: batch.get("raw_content", ""),
            f"对账明细_{stamp}.csv": detail_buffer.getvalue(),
            f"对账汇总_{stamp}.csv": summary_buffer.getvalue(),
        }

    def _remove_archive(self, archive_id: str) -> None:
        directory = self.archives.archive_root / archive_id
        if not directory.exists():
            return
        for file in directory.iterdir():
            file.unlink()
        directory.rmdir()


import_service = ChemicalImportService()
