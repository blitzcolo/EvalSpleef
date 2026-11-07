"""Cost ledger entries (TECH-DESIGN section 11 ``cost_ledger``).

Every entry records one cost figure of a batch: the kind of resource, who
produced the figure (``source``), the USD amount, the metered quantity with
its unit, the period it covers, and whether reconciliation has confirmed it
against vendor billing.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self

from evalspleef.control.store.db import Row

__all__ = ["CostEntry", "CostKind"]


class CostKind(StrEnum):
    """Resource class of a cost figure (TECH-DESIGN 11 ``kind``)."""

    TOKENS = "tokens"
    GPU = "gpu"
    SANDBOX = "sandbox"
    INFRA = "infra"

    @classmethod
    def parse(cls, value: str) -> Self:
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(f"unknown cost kind: {value!r}") from exc


@dataclass(frozen=True)
class CostEntry:
    """One row of the batch cost ledger."""

    entry_id: str
    batch_id: str
    kind: CostKind
    source: str
    amount_usd: float
    quantity: float
    unit: str
    period: str
    reconciled: bool = False

    def params(self) -> tuple[Any, ...]:
        return (
            self.entry_id,
            self.batch_id,
            str(self.kind),
            self.source,
            self.amount_usd,
            self.quantity,
            self.unit,
            self.period,
            1 if self.reconciled else 0,
        )

    @classmethod
    def from_row(cls, row: Row) -> Self:
        return cls(
            entry_id=str(row["entry_id"]),
            batch_id=str(row["batch_id"]),
            kind=CostKind.parse(str(row["kind"])),
            source=str(row["source"]),
            amount_usd=float(row["amount_usd"]),
            quantity=float(row["quantity"]),
            unit=str(row["unit"]),
            period=str(row["period"]),
            reconciled=bool(row["reconciled"]),
        )
