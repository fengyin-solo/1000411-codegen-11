"""药剂出入业务规则：状态流转、字段校验、结存重算与导入对账都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services import chemical_ledger as ledger
from app.services.chemical_import import import_service
from app.store import store

MODULE = "chemical"
REQUIRED_FIELDS = ["单据编号", "药剂名称", "规格型号"]
STATUS_ORDER = ["待审核", "已审核", "已出入库", "已作废"]
ACTION_RULES = {"审核单据": "已审核", "确认出入库": "已出入库", "作废单据": "已作废"}
NEGATIVE_ACTIONS = ["作废单据"]


class ChemicalService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("单据编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [self._present(row) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        return self._present(row) if row is not None else None

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"药剂单据 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于药剂出入可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        # 记账/作废后按统一口径重算全量结存，保证单据与结存始终对得上；动作本身与状态流转不变。
        ledger.recompute_balances(store.rows(MODULE))
        return self._present(entry), f"药剂单据已{action}"

    def recalc_balances(self) -> dict[str, Any]:
        """手动结存重算：重放有效单据流水，并与重算前逐单核对，返回对账结果。"""
        rows = store.rows(MODULE)
        before = {
            str(row.get("单据编号", "")): str(row.get("结存数量", ""))
            for row in rows
            if row.get("status") == ledger.POSTED_STATUS
        }
        ledger.recompute_balances(rows)
        mismatches = ledger.verify_ledger(rows)
        changed = [
            {"单据编号": code, "重算前": old, "重算后": next(
                (str(row.get("结存数量", "")) for row in rows if str(row.get("单据编号", "")) == code),
                "",
            )}
            for code, old in before.items()
            if next(
                (str(row.get("结存数量", "")) for row in rows if str(row.get("单据编号", "")) == code),
                "",
            ) != old
        ]
        closing = ledger.closing_balances(rows)
        return {
            "ok": not mismatches,
            "message": "结存重算完成，明细与结存一致" if not mismatches else f"重算发现 {len(mismatches)} 项不一致",
            "changed": changed,
            "mismatches": mismatches,
            "closing_balances": [
                {"药剂名称": key[0], "规格型号": key[1], "期末结存": ledger.format_number(value)}
                for key, value in sorted(closing.items())
            ],
        }

    # ---------- 单据导入（差异预览 → 确认落库 → 对账归档） ----------

    def preview_import(self, content: str) -> dict[str, Any]:
        return import_service.preview(content, store.rows(MODULE))

    def commit_import(
        self, token: str, raw_content: str = "", operator: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        def replace_rows(new_rows: list[dict[str, Any]]) -> None:
            store.replace_rows(MODULE, new_rows)

        return import_service.commit(
            token,
            store.rows(MODULE),
            replace_rows=replace_rows,
            raw_content=raw_content,
            operator=operator,
        )

    def _present(self, row: dict[str, Any]) -> dict[str, Any]:
        """对外展示补一份出入方向：历史单据没有该字段时按入库口径呈现。"""
        presented = dict(row)
        presented["出入方向"] = ledger.direction_of(row)
        return presented
