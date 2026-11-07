"""Batch cost ledger: recording, vendor billing import, reconciliation (TECH-DESIGN 6.7).

The ledger holds two sides of every batch's cost. Platform-recorded entries
(``source`` naming the component that metered them: gateway, endpoint manager,
sandbox provider, host) accumulate while the batch runs. Vendor billing is
imported with :meth:`CostLedger.import_novita_billing`, which maps the billing
API's ``product_category`` values onto :class:`CostKind` and stores the lines
with :data:`NOVITA_SOURCE`. :meth:`CostLedger.reconcile` then compares both
sides per kind: ``|Σ ledger − Σ bill| / Σ bill`` must stay within the
tolerance (5 % by default).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evalspleef.core.cost import allocate_gpu_cost as _allocate_gpu_cost

from ..store.db import INTEGRITY_ERRORS, Database
from .entries import CostEntry, CostKind

__all__ = [
    "NOVITA_SOURCE",
    "CostLedger",
    "KindReconciliation",
    "ReconcileReport",
]

#: Source tag of entries imported from Novita billing; reconciliation compares
#: every other source against this side.
NOVITA_SOURCE = "novita"

#: Novita billing ``product_category`` -> cost kind. Categories outside this
#: table (future product lines) are skipped on import, never misclassified.
#: See PRD §8.1 for the confirmed categories.
_PRODUCT_CATEGORY_KINDS: Mapping[str, CostKind] = {
    "llm_dedicated_endpoint": CostKind.GPU,
    "llm": CostKind.TOKENS,
    "sandbox": CostKind.SANDBOX,
    "storage": CostKind.INFRA,
    "network": CostKind.INFRA,
}


@dataclass(frozen=True)
class KindReconciliation:
    """One cost kind's ledger-vs-bill comparison for a batch."""

    kind: CostKind
    ledger_usd: float
    bill_usd: float
    difference_usd: float
    relative_difference: float | None
    """``|ledger − bill| / bill``; None when the bill side is zero."""
    ok: bool


@dataclass(frozen=True)
class ReconcileReport:
    """Per-kind reconciliation result for one batch."""

    batch_id: str
    tolerance: float
    kinds: tuple[KindReconciliation, ...]

    @property
    def ok(self) -> bool:
        """True when every kind is within tolerance (a zero bill must match a zero ledger)."""
        return all(kind.ok for kind in self.kinds)


class CostLedger:
    """Cost figures and vendor-bill reconciliation for one relational store."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record(self, entry: CostEntry) -> None:
        """Append one cost entry; duplicate ids are rejected."""
        try:
            await self._db.execute(
                "INSERT INTO cost_ledger (entry_id, batch_id, kind, source, amount_usd, "
                "quantity, unit, period, reconciled) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                entry.params(),
            )
        except INTEGRITY_ERRORS as exc:
            raise ValueError(f"cost entry {entry.entry_id!r} already exists") from exc

    async def entries(
        self,
        batch_id: str,
        *,
        kind: CostKind | None = None,
        source: str | None = None,
    ) -> list[CostEntry]:
        """Return a batch's entries, optionally filtered."""
        sql = "SELECT * FROM cost_ledger WHERE batch_id = %s"
        params: list[Any] = [batch_id]
        if kind is not None:
            sql += " AND kind = %s"
            params.append(str(kind))
        if source is not None:
            sql += " AND source = %s"
            params.append(source)
        sql += " ORDER BY entry_id"
        rows = await self._db.fetch(sql, params)
        return [CostEntry.from_row(row) for row in rows]

    async def mark_reconciled(
        self, batch_id: str, *, kinds: Sequence[CostKind] | None = None
    ) -> int:
        """Flag entries as reconciled; returns how many rows were flagged."""
        if kinds is None:
            cursor_check = await self._db.fetch(
                "SELECT entry_id FROM cost_ledger WHERE batch_id = %s AND reconciled = 0",
                (batch_id,),
            )
            await self._db.execute(
                "UPDATE cost_ledger SET reconciled = 1 WHERE batch_id = %s AND reconciled = 0",
                (batch_id,),
            )
            return len(cursor_check)
        total = 0
        for kind in kinds:
            rows = await self._db.fetch(
                "SELECT entry_id FROM cost_ledger WHERE batch_id = %s AND kind = %s "
                "AND reconciled = 0",
                (batch_id, str(kind)),
            )
            await self._db.execute(
                "UPDATE cost_ledger SET reconciled = 1 WHERE batch_id = %s AND kind = %s",
                (batch_id, str(kind)),
            )
            total += len(rows)
        return total

    async def import_novita_billing(
        self,
        payload: str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        batch_id: str,
        period: str,
        id_prefix: str = "novita",
    ) -> list[CostEntry]:
        """Import Novita billing lines for ``batch_id`` and return the stored entries.

        ``payload`` is the billing API JSON (PRD FR-OPS-04, §8.1): either the
        decoded response with an ``items``/``charges``/``transactions`` list,
        or the list itself. Every line carries a ``product_category`` (also
        accepted as ``productCategory``), an USD ``amount``, and an optional
        ``quantity``/``unit``. Known categories map onto :class:`CostKind`;
        unknown categories are skipped rather than misclassified.

        Lines are stored as entries with source :data:`NOVITA_SOURCE`; ids are
        derived from the line's own transaction id so re-importing a bill does
        not create duplicates (and raises instead, keeping the import visible).
        """
        items = self._billing_items(payload)
        imported: list[CostEntry] = []
        for index, item in enumerate(items):
            category = str(item.get("product_category", item.get("productCategory", ""))).lower()
            kind = _PRODUCT_CATEGORY_KINDS.get(category)
            if kind is None:
                continue
            transaction_id = str(item.get("transaction_id") or item.get("id") or f"line-{index}")
            amount = float(item.get("amount", 0.0))
            quantity = float(item.get("quantity", 0.0))
            unit = str(item.get("unit") or "")
            entry = CostEntry(
                entry_id=f"{id_prefix}:{transaction_id}",
                batch_id=batch_id,
                kind=kind,
                source=NOVITA_SOURCE,
                amount_usd=amount,
                quantity=quantity,
                unit=unit,
                period=period,
                reconciled=False,
            )
            await self.record(entry)
            imported.append(entry)
        return imported

    @staticmethod
    def _billing_items(
        payload: str | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        """Normalize the billing payload to a list of line mappings."""
        if isinstance(payload, str):
            payload = json.loads(payload)
        if isinstance(payload, Mapping):
            for key in ("items", "charges", "transactions", "data"):
                value = payload.get(key)
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
                    return [item for item in value if isinstance(item, Mapping)]
            raise ValueError("billing payload has no items/charges/transactions list")
        if isinstance(payload, Sequence):
            return [item for item in payload if isinstance(item, Mapping)]
        raise TypeError(f"unsupported billing payload type: {type(payload).__name__}")

    async def total_usd(self, batch_id: str, *, kind: CostKind | None = None) -> float:
        """Sum of a batch's recorded amounts, optionally per kind (all sources)."""
        entries = await self.entries(batch_id, kind=kind)
        return sum(entry.amount_usd for entry in entries)

    async def reconcile(self, batch_id: str, *, tolerance: float = 0.05) -> ReconcileReport:
        """Compare platform-recorded costs against the imported Novita bill.

        For every :class:`CostKind` present on either side, ``ok`` means
        ``|ledger − bill| / bill <= tolerance``; a zero bill must match a zero
        ledger. Entries flagged ``reconciled`` are excluded from neither side —
        the flag records that a past comparison confirmed them.
        """
        if tolerance < 0:
            raise ValueError(f"tolerance must be non-negative: {tolerance!r}")
        all_entries = await self.entries(batch_id)
        per_kind: dict[CostKind, KindReconciliation] = {}
        for kind in CostKind:
            ledger_usd = sum(
                entry.amount_usd
                for entry in all_entries
                if entry.kind is kind and entry.source != NOVITA_SOURCE
            )
            bill_usd = sum(
                entry.amount_usd
                for entry in all_entries
                if entry.kind is kind and entry.source == NOVITA_SOURCE
            )
            difference = ledger_usd - bill_usd
            if bill_usd > 0:
                relative = abs(difference) / bill_usd
                ok = relative <= tolerance
            else:
                relative = None
                ok = ledger_usd == 0
            per_kind[kind] = KindReconciliation(
                kind=kind,
                ledger_usd=ledger_usd,
                bill_usd=bill_usd,
                difference_usd=difference,
                relative_difference=relative,
                ok=ok,
            )
        return ReconcileReport(
            batch_id=batch_id, tolerance=tolerance, kinds=tuple(per_kind.values())
        )

    async def allocate_gpu(
        self, batch_id: str, tokens_by_trial: Mapping[str, int]
    ) -> dict[str, float]:
        """Split the batch's GPU cost across trials by token share (TECH-DESIGN 6.7).

        ``trial.gpu_alloc = batch.gpu_cost × trial_tokens / batch_tokens`` for
        trials on the same endpoint; the per-endpoint split is the caller's job
        (group ``tokens_by_trial`` accordingly). Delegates to
        :func:`evalspleef.core.cost.allocate_gpu_cost`.
        """
        return _allocate_gpu_cost(
            await self.total_usd(batch_id, kind=CostKind.GPU), tokens_by_trial
        )
