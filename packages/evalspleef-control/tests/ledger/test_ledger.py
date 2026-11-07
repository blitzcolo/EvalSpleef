"""Cost ledger tests: recording, Novita billing import, reconciliation, GPU allocation."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from evalspleef.control.ledger import (
    NOVITA_SOURCE,
    CostEntry,
    CostKind,
    CostLedger,
)
from evalspleef.control.store import connect, migrate
from evalspleef.control.store.db import Database

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


async def test_import_novita_billing_maps_product_categories(ledger: CostLedger) -> None:
    payload = {
        "items": [
            {
                "transaction_id": "t-1",
                "product_category": "llm_dedicated_endpoint",
                "amount": 40.0,
                "quantity": 20.0,
                "unit": "gpu·h",
            },
            {
                "transaction_id": "t-2",
                "product_category": "llm",
                "amount": 1.0,
                "quantity": 1.0,
                "unit": "mtok",
            },
            {
                "transaction_id": "t-3",
                "product_category": "sandbox",
                "amount": 0.12,
                "quantity": 7200.0,
                "unit": "vcpu·s",
            },
            {"transaction_id": "t-4", "product_category": "storage", "amount": 0.05},
            {"transaction_id": "t-5", "product_category": "some_future_product", "amount": 99.0},
        ]
    }
    imported = await ledger.import_novita_billing(payload, batch_id=BATCH, period=PERIOD)
    # The unknown future category is skipped, not misclassified.
    assert sorted(e.kind for e in imported) == [
        CostKind.GPU,
        CostKind.INFRA,
        CostKind.SANDBOX,
        CostKind.TOKENS,
    ]
    assert all(e.source == NOVITA_SOURCE for e in imported)
    stored = await ledger.entries(BATCH, source=NOVITA_SOURCE)
    assert {e.entry_id for e in stored} == {
        "novita:t-1",
        "novita:t-2",
        "novita:t-3",
        "novita:t-4",
    }
    # Re-importing the same bill does not create duplicates.
    with pytest.raises(ValueError, match="already exists"):
        await ledger.import_novita_billing(payload, batch_id=BATCH, period=PERIOD)


async def test_import_accepts_list_and_string_payloads(ledger: CostLedger) -> None:
    lines = [
        {"transaction_id": "x-1", "productCategory": "llm", "amount": 3.0},
    ]
    from_list = await ledger.import_novita_billing(
        lines, batch_id=BATCH, period=PERIOD, id_prefix="a"
    )
    assert len(from_list) == 1
    # camelCase productCategory is accepted too.
    from_str = await ledger.import_novita_billing(
        json.dumps({"transactions": lines}), batch_id=BATCH, period=PERIOD, id_prefix="b"
    )
    assert len(from_str) == 1
    with pytest.raises(ValueError, match="no items"):
        await ledger.import_novita_billing({"nope": []}, batch_id=BATCH, period=PERIOD)


async def test_reconcile_within_and_beyond_tolerance(ledger: CostLedger) -> None:
    # Platform-recorded side.
    await ledger.record(entry("g-1", CostKind.TOKENS, 1.00))
    await ledger.record(entry("g-2", CostKind.GPU, 38.0, source="endpoint-manager"))
    await ledger.record(entry("g-3", CostKind.SANDBOX, 0.12, source="sandbox-provider"))
    # Vendor bill: tokens exact, GPU 5% high, sandbox missing on the bill.
    await ledger.import_novita_billing(
        {
            "items": [
                {"transaction_id": "b-1", "product_category": "llm", "amount": 1.00},
                {
                    "transaction_id": "b-2",
                    "product_category": "llm_dedicated_endpoint",
                    "amount": 40.0,
                },
            ]
        },
        batch_id=BATCH,
        period=PERIOD,
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
