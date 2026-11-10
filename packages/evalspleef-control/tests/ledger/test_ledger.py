"""Cost ledger tests: recording, billing import, reconciliation, GPU allocation."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from evalspleef.control.ledger import (
    BILL_SOURCE_PREFIX,
    CostEntry,
    CostKind,
    CostLedger,
)
from evalspleef.control.store import connect, migrate
from evalspleef.control.store.db import Database
from evalspleef.spi.model import BillLine

BATCH = "2026-11-b1"
PERIOD = "2026-11"


@pytest_asyncio.fixture
async def db() -> AsyncIterator[Database]:
    database = await connect("sqlite:///:memory:")
    await migrate(database)
    yield database
    await database.close()


@pytest_asyncio.fixture
async def ledger(db: Database) -> CostLedger:
    return CostLedger(db)


def entry(
    entry_id: str,
    kind: CostKind,
    amount_usd: float,
    *,
    source: str = "gateway",
    quantity: float = 1.0,
    unit: str = "mtok",
    reconciled: bool = False,
) -> CostEntry:
    return CostEntry(
        entry_id=entry_id,
        batch_id=BATCH,
        kind=kind,
        source=source,
        amount_usd=amount_usd,
        quantity=quantity,
        unit=unit,
        period=PERIOD,
        reconciled=reconciled,
    )


async def test_record_and_list_entries(ledger: CostLedger) -> None:
    await ledger.record(entry("e-1", CostKind.TOKENS, 1.25, quantity=2.5, unit="mtok"))
    await ledger.record(
        entry("e-2", CostKind.GPU, 40.0, source="endpoint-manager", quantity=20.0, unit="gpu·h")
    )
    await ledger.record(entry("e-3", CostKind.TOKENS, 0.75))

    entries = await ledger.entries(BATCH)
    assert [e.entry_id for e in entries] == ["e-1", "e-2", "e-3"]
    tokens = await ledger.entries(BATCH, kind=CostKind.TOKENS)
    assert [e.entry_id for e in tokens] == ["e-1", "e-3"]
    gateway = await ledger.entries(BATCH, source="gateway")
    assert [e.entry_id for e in gateway] == ["e-1", "e-3"]

    with pytest.raises(ValueError, match="already exists"):
        await ledger.record(entry("e-1", CostKind.TOKENS, 9.9))


async def test_mark_reconciled(ledger: CostLedger) -> None:
    await ledger.record(entry("e-1", CostKind.TOKENS, 1.0))
    await ledger.record(entry("e-2", CostKind.GPU, 2.0))
    assert await ledger.mark_reconciled(BATCH, kinds=[CostKind.TOKENS]) == 1
    entries = await ledger.entries(BATCH)
    assert [e.reconciled for e in entries] == [True, False]
    assert await ledger.mark_reconciled(BATCH) == 1
    assert all(e.reconciled for e in await ledger.entries(BATCH))


def bill_line(
    ref: str,
    kind: str,
    amount_usd: float,
    *,
    quantity: float = 0.0,
    unit: str = "",
    period: str = "",
) -> BillLine:
    return BillLine(
        kind=kind,
        amount_usd=amount_usd,
        quantity=quantity,
        unit=unit,
        period=period,
        ref=ref,
    )


async def test_import_bill_maps_kinds_to_entries(ledger: CostLedger) -> None:
    lines = [
        bill_line("t-1", "gpu", 40.0, quantity=20.0, unit="gpu·h"),
        bill_line("t-2", "tokens", 1.0, quantity=1.0, unit="mtok"),
        bill_line("t-3", "sandbox", 0.12, quantity=7200.0, unit="vcpu·s"),
        bill_line("t-4", "infra", 0.05),
        bill_line("t-5", "some_future_product", 99.0),
    ]
    imported = await ledger.import_bill(lines, batch_id=BATCH, period=PERIOD, account="acme")
    # The unknown future kind is skipped, not misclassified.
    assert sorted(e.kind for e in imported) == [
        CostKind.GPU,
        CostKind.INFRA,
        CostKind.SANDBOX,
        CostKind.TOKENS,
    ]
    assert all(e.source == f"{BILL_SOURCE_PREFIX}acme" for e in imported)
    stored = await ledger.entries(BATCH, source=f"{BILL_SOURCE_PREFIX}acme")
    assert {e.entry_id for e in stored} == {
        "acme:t-1",
        "acme:t-2",
        "acme:t-3",
        "acme:t-4",
    }
    assert all(e.period == PERIOD for e in stored)
    # Re-importing the same bill does not create duplicates.
    with pytest.raises(ValueError, match="already exists"):
        await ledger.import_bill(lines, batch_id=BATCH, period=PERIOD, account="acme")


async def test_import_bill_keeps_the_lines_own_period(ledger: CostLedger) -> None:
    imported = await ledger.import_bill(
        [bill_line("p-1", "tokens", 3.0, period="2026-10")],
        batch_id=BATCH,
        period="2026-11",
        account="acme",
    )
    assert imported[0].period == "2026-10"
    fallback = await ledger.import_bill(
        [bill_line("p-2", "tokens", 3.0)], batch_id=BATCH, period="2026-11", account="acme"
    )
    assert fallback[0].period == "2026-11"


async def test_reconcile_within_and_beyond_tolerance(ledger: CostLedger) -> None:
    # Platform-recorded side.
    await ledger.record(entry("g-1", CostKind.TOKENS, 1.00))
    await ledger.record(entry("g-2", CostKind.GPU, 38.0, source="endpoint-manager"))
    await ledger.record(entry("g-3", CostKind.SANDBOX, 0.12, source="sandbox-provider"))
    # Vendor bill: tokens exact, GPU 5% high, sandbox missing on the bill.
    await ledger.import_bill(
        [
            bill_line("b-1", "tokens", 1.00),
            bill_line("b-2", "gpu", 40.0),
        ],
        batch_id=BATCH,
        period=PERIOD,
        account="acme",
    )

    report = await ledger.reconcile(BATCH, tolerance=0.05)
    by_kind = {k.kind: k for k in report.kinds}
    assert by_kind[CostKind.TOKENS].ok
    assert by_kind[CostKind.TOKENS].difference_usd == 0.0
    # |38 − 40| / 40 = 5%: exactly at the default tolerance.
    gpu = by_kind[CostKind.GPU]
    assert gpu.ledger_usd == 38.0 and gpu.bill_usd == 40.0
    assert gpu.relative_difference == pytest.approx(0.05)
    assert gpu.ok
    # Sandbox: ledger has 0.12, bill has nothing.
    sandbox = by_kind[CostKind.SANDBOX]
    assert sandbox.bill_usd == 0.0 and sandbox.relative_difference is None
    assert not sandbox.ok
    assert not report.ok

    # A stricter tolerance fails the GPU kind.
    strict = await ledger.reconcile(BATCH, tolerance=0.04)
    assert not strict.ok
    assert not {k.kind: k for k in strict.kinds}[CostKind.GPU].ok


async def test_reconcile_zero_bill_requires_zero_ledger(ledger: CostLedger) -> None:
    report = await ledger.reconcile(BATCH)
    assert all(kind.ok for kind in report.kinds)
    await ledger.record(entry("i-1", CostKind.INFRA, 0.10))
    report = await ledger.reconcile(BATCH)
    assert not {k.kind: k for k in report.kinds}[CostKind.INFRA].ok

    with pytest.raises(ValueError, match="tolerance"):
        await ledger.reconcile(BATCH, tolerance=-0.1)


async def test_allocate_gpu_splits_by_token_share(ledger: CostLedger) -> None:
    await ledger.record(entry("g-1", CostKind.GPU, 100.0, source="endpoint-manager"))
    allocation = await ledger.allocate_gpu(BATCH, {"t-01": 750_000, "t-02": 250_000})
    assert allocation == pytest.approx({"t-01": 75.0, "t-02": 25.0})
    assert sum(allocation.values()) == pytest.approx(100.0)
