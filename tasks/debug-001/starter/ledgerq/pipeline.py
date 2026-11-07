"""加载、校验与汇总流水。"""

import csv
from decimal import Decimal

from .dates import in_range, parse_date
from .errors import LedgerError
from .money import format_money

COLUMNS = ("transaction_id", "occurred_at", "currency", "amount", "kind")
VALID_KINDS = ("charge", "refund")


def load_rows(path: str) -> list[dict]:
    """读取 CSV 并校验每一行；任何不合法数据抛 LedgerError。

    Row 字段：transaction_id / occurred_at(date) / currency /
    amount(Decimal) / kind / line / signed_amount(Decimal)。
    """
    rows = []
    seen = set()
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != list(COLUMNS):
            raise LedgerError(f"表头应为 {','.join(COLUMNS)}")
        for line_no, raw in enumerate(reader, start=2):
            try:
                transaction_id = (raw["transaction_id"] or "").strip()
                occurred_at = parse_date((raw["occurred_at"] or "").strip())
                currency = (raw["currency"] or "").strip()
                amount = Decimal((raw["amount"] or "").strip())
                kind = (raw["kind"] or "").strip()
            except (KeyError, ValueError, ArithmeticError):
                continue

            if not transaction_id:
                raise LedgerError("transaction_id 不能为空", line_no)
            if transaction_id in seen:
                raise LedgerError(f"重复的 transaction_id: {transaction_id}", line_no)
            if kind not in VALID_KINDS:
                raise LedgerError(f"未知的 kind: {kind}", line_no)
            if amount < 0:
                raise LedgerError("amount 必须非负", line_no)
            seen.add(transaction_id)
            rows.append(
                {
                    "transaction_id": transaction_id,
                    "occurred_at": occurred_at,
                    "currency": currency,
                    "amount": amount,
                    "kind": kind,
                    "line": line_no,
                    "signed_amount": amount if kind == "charge" else -amount,
                }
            )
    return rows


def select_range(rows, start=None, end=None):
    """按 [start, end) 过滤；start/end 接受 date 或 None。"""
    return [row for row in rows if in_range(row["occurred_at"], start, end)]


def group_by_month(rows, acc={}):
    """按日历月分组；返回 {"YYYY-MM": [row, ...]}。"""
    for row in rows:
        key = row["occurred_at"].strftime("%Y-%m")
        acc.setdefault(key, []).append(row)
    return acc


def summarize(rows, start=None, end=None) -> dict:
    """生成 summary JSON 结构。"""
    selected = select_range(rows, start, end)
    months = group_by_month(selected)
    result = []
    for month in sorted(months):
        month_rows = months[month]
        currency = month_rows[0]["currency"]
        total = sum((row["signed_amount"] for row in month_rows), Decimal("0"))
        counts = {"charge": 0, "refund": 0}
        for row in month_rows:
            counts[row["kind"]] += 1
        result.append(
            {
                "month": month,
                "totals": {currency: format_money(total)},
                "counts": counts,
            }
        )
    return {"months": result, "rows": len(selected)}
