"""Batch cost ledger: recording, vendor billing import, reconciliation (TECH-DESIGN 6.7).

The ledger holds two sides of every batch's cost. Platform-recorded entries
(``source`` naming the component that metered them: gateway, endpoint manager,
sandbox provider, host) accumulate while the batch runs. Vendor billing arrives
as vendor-free :class:`~evalspleef.spi.model.BillLine` values parsed by the model
provider's own ``parse_bill`` and is stored with :meth:`CostLedger.import_bill`
under a ``bill:<account>`` source. :meth:`CostLedger.reconcile` then compares
both sides per kind: ``|Σ ledger − Σ bill| / Σ bill`` must stay within the
tolerance (5 % by default).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from evalspleef.core.cost import allocate_gpu_cost as _allocate_gpu_cost
from evalspleef.spi.model import BillLine

from ..store.db import INTEGRITY_ERRORS, Database
from .entries import CostEntry, CostKind

__all__ = [
    "BILL_SOURCE_PREFIX",
    "CostLedger",
    "KindReconciliation",
    "ReconcileReport",
]

#: Prefix of the ``source`` tag of entries imported from a provider billing export
#: (``bill:<account>``); reconciliation compares every other source against these.
BILL_SOURCE_PREFIX = "bill:"


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
        params: list[str] = [batch_id]
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

    async def import_bill(
        self,
        lines: Sequence[BillLine],
        *,
        batch_id: str,
        period: str,
        account: str,
    ) -> list[CostEntry]:
        """Import parsed billing lines for ``batch_id`` and return the stored entries.

        ``lines`` come from the account's ``ModelEndpointProvider.parse_bill`` and
        carry the platform cost-kind vocabulary; kinds outside it are skipped,
        never misclassified. Lines are stored with source ``bill:<account>``; ids
        are derived from the line's own reference so re-importing a bill does not
        create duplicates (and raises instead, keeping the import visible).
        """
        imported: list[CostEntry] = []
        for index, line in enumerate(lines):
            try:
                kind = CostKind.parse(line.kind)
            except ValueError:
                continue
            entry = CostEntry(
                entry_id=f"{account}:{line.ref or f'line-{index}'}",
                batch_id=batch_id,
                kind=kind,
                source=f"{BILL_SOURCE_PREFIX}{account}",
                amount_usd=line.amount_usd,
                quantity=line.quantity,
                unit=line.unit,
                period=line.period or period,
                reconciled=False,
            )
            await self.record(entry)
            imported.append(entry)
        return imported

    async def total_usd(self, batch_id: str, *, kind: CostKind | None = None) -> float:
        """Sum of a batch's recorded amounts, optionally per kind (all sources)."""
        entries = await self.entries(batch_id, kind=kind)
        return sum(entry.amount_usd for entry in entries)

    async def reconcile(self, batch_id: str, *, tolerance: float = 0.05) -> ReconcileReport:
        """Compare platform-recorded costs against the imported billing export.

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
                if entry.kind is kind and not entry.source.startswith(BILL_SOURCE_PREFIX)
            )
            bill_usd = sum(
                entry.amount_usd
                for entry in all_entries
                if entry.kind is kind and entry.source.startswith(BILL_SOURCE_PREFIX)
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
