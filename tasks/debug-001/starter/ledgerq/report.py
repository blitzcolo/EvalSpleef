"""CSV / 文本导出。"""

import csv

from .money import format_money

EXPORT_COLUMNS = (
    "transaction_id",
    "occurred_at",
    "currency",
    "amount",
    "signed_amount",
    "kind",
)


def export_csv(rows, path: str) -> None:
    """按 (occurred_at, transaction_id) 升序导出 CSV。"""
    ordered = sorted(rows, key=lambda row: (row["occurred_at"], row["transaction_id"]))
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(EXPORT_COLUMNS)
        for row in ordered:
            writer.writerow(
                [
                    row["transaction_id"],
                    row["occurred_at"].isoformat(),
                    row["currency"],
                    format_money(row["amount"]),
                    format_money(row["signed_amount"]),
                    row["kind"],
                ]
            )
