"""药剂台账对账口径：单据与结存的换算、重算、核对都以这里为唯一来源。

约定：
- 入库数量按正数参与结存，出库数量按负数参与结存；
- 只有「已出入库」单据参与结存累加，待审核/已审核单据不影响结存；
- 已作废单据不参与累加，展示的结存沿用此前最后一张有效单据的结存；
- 同一药剂（药剂名称 + 规格型号）按单据编号排序做逐单流水结存。
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

POSTED_STATUS = "已出入库"
VOID_STATUS = "已作废"
INBOUND = "入库"
OUTBOUND = "出库"
DIRECTION_LABELS = {INBOUND, OUTBOUND}


def signed_quantity(values: dict[str, Any]) -> Decimal:
    """按出入方向换算成带符号数量：入库为正、出库为负。"""
    quantity = to_decimal(values.get("出入数量"))
    direction = str(values.get("出入方向") or INBOUND).strip()
    return quantity if direction != OUTBOUND else -quantity


def to_decimal(value: Any) -> Decimal:
    """把导入/存量里的数量统一转成 Decimal；无法识别时按 0 处理，不抛异常。"""
    if value is None or str(value).strip() == "":
        return Decimal("0")
    text = str(value).strip().replace(",", "")
    try:
        return Decimal(text)
    except InvalidOperation:
        return Decimal("0")


def format_number(value: Decimal) -> str:
    """结存/数量展示：整数不带小数点与指数，非整数去掉尾零。"""
    if value == value.to_integral_value():
        return str(value.to_integral())
    return format(value, "f").rstrip("0").rstrip(".")


def _posting_order(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: (str(row.get("单据编号") or ""), int(row.get("id", 0))))


def recompute_balances(rows: list[dict[str, Any]]) -> None:
    """全量重算结存数量并直接写回各行。

    按「药剂名称 + 规格型号」分组，按单据编号排序逐单累加；
    待处理单据不改结存，已作废单据不参与累加。结果落回各行的「结存数量」，
    保证任何时刻列表里的结存与明细流水对得上。
    """
    running: dict[tuple[str, str], Decimal] = {}
    for row in _posting_order(rows):
        key = (str(row.get("药剂名称") or ""), str(row.get("规格型号") or ""))
        balance = running.get(key, Decimal("0"))
        if row.get("status") == POSTED_STATUS:
            balance += signed_quantity(row)
            running[key] = balance
        row["结存数量"] = format_number(balance)


def verify_ledger(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """核对结存与明细：逐药剂重放流水，与行内结存比对，列出不一致项。"""
    expected: dict[tuple[str, str], Decimal] = {}
    mismatches: list[dict[str, Any]] = []
    for row in _posting_order(rows):
        if row.get("status") != POSTED_STATUS:
            continue
        key = (str(row.get("药剂名称") or ""), str(row.get("规格型号") or ""))
        balance = expected.get(key, Decimal("0")) + signed_quantity(row)
        expected[key] = balance
        actual = to_decimal(row.get("结存数量"))
        if actual != balance:
            mismatches.append({
                "单据编号": row.get("单据编号"),
                "药剂名称": key[0],
                "规格型号": key[1],
                "行内结存": format_number(actual),
                "应有结存": format_number(balance),
            })
    return mismatches


def closing_balances(rows: list[dict[str, Any]]) -> dict[tuple[str, str], Decimal]:
    """按药剂汇总期末结存：有效单据带符号数量之和。"""
    closing: dict[tuple[str, str], Decimal] = {}
    for row in rows:
        if row.get("status") != POSTED_STATUS:
            continue
        key = (str(row.get("药剂名称") or ""), str(row.get("规格型号") or ""))
        closing[key] = closing.get(key, Decimal("0")) + signed_quantity(row)
    return closing


def negative_balances(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """找出重算后结存为负的药剂，供导入预览给出风险提示。"""
    result = []
    for (name, spec), balance in closing_balances(rows).items():
        if balance < 0:
            result.append({
                "药剂名称": name,
                "规格型号": spec,
                "期末结存": format_number(balance),
            })
    return result


def direction_of(row: dict[str, Any]) -> str:
    """展示用：给没有方向字段的历史单据补一个入库口径。"""
    direction = str(row.get("出入方向") or "").strip()
    return direction if direction in DIRECTION_LABELS else INBOUND
