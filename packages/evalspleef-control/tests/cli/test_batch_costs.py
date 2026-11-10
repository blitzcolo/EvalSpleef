"""spleef batch costs --bill/--account: provider billing import and reconciliation.

The bill file is parsed by the account's model provider (``parse_bill``); the
ledger stores the lines and reconciles them against the platform-recorded costs.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalspleef.control.cli.app import app
from evalspleef.control.ledger import CostEntry, CostKind, CostLedger
from evalspleef.control.store import connect, migrate

BATCH = "2026-11-b1"


def _flat(output: str) -> str:
    """Output with the terminal box wrapping collapsed, for substring assertions."""
    return " ".join(output.split())


BILL = json.dumps(
    {
        "items": [
            {"transaction_id": "b-1", "product_category": "llm", "amount": 1.00},
            {
                "transaction_id": "b-2",
                "product_category": "llm_dedicated_endpoint",
                "amount": 40.0,
            },
        ]
    }
)


@pytest.fixture
def billed_env(env: Path) -> Iterator[Path]:
    """The fixture config plus a model account whose provider can parse bills."""
    providers = env / "providers.yaml"
    providers.write_text(
        providers.read_text(encoding="utf-8").replace(
            "sandbox_instances:",
            "  novita:\n    adapter: novita-dedicated\n\nsandbox_instances:",
        ),
        encoding="utf-8",
    )
    yield env


def _record_platform_costs(env: Path) -> None:
    """Two ledger entries a batch run would record (tokens exact, GPU 5% under the bill)."""

    async def run() -> None:
        db = await connect(f"sqlite:///{env.parent}/store.sqlite3")
        await migrate(db)
        ledger = CostLedger(db)
        await ledger.record(
            CostEntry(
                entry_id="g-1",
                batch_id=BATCH,
                kind=CostKind.TOKENS,
                source="gateway",
                amount_usd=1.00,
                quantity=1.0,
                unit="mtok",
                period="2026-11",
            )
        )
        await ledger.record(
            CostEntry(
                entry_id="g-2",
                batch_id=BATCH,
                kind=CostKind.GPU,
                source="endpoint-manager",
                amount_usd=38.0,
                quantity=19.0,
                unit="gpu·h",
                period="2026-11",
            )
        )
        await db.close()

    asyncio.run(run())


def _bill_entries(env: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(env.parent / "store.sqlite3") as db:
        return db.execute(
            "SELECT entry_id, kind, source FROM cost_ledger WHERE batch_id = ? ORDER BY entry_id",
            (BATCH,),
        ).fetchall()


def test_bill_import_reconciles(cli_runner: CliRunner, args: list[str], billed_env: Path) -> None:
    _record_platform_costs(billed_env)
    bill = billed_env / "bill.json"
    bill.write_text(BILL, encoding="utf-8")
    providers = args[args.index("--providers") + 1]

    result = cli_runner.invoke(
        app,
        [
            "batch",
            "costs",
            "--batch-id",
            BATCH,
            "--bill",
            str(bill),
            "--account",
            "novita",
            "--providers",
            providers,
        ],
    )

    assert result.exit_code == 0, result.output
    assert "tokens" in result.output and "gpu" in result.output
    assert "MISMATCH" not in result.output
    # The import is visible in the ledger under the account-tagged source and id.
    assert ("novita:b-1", "tokens", "bill:novita") in _bill_entries(billed_env)


def test_account_without_billing_capability_is_refused(
    cli_runner: CliRunner, args: list[str], billed_env: Path
) -> None:
    _record_platform_costs(billed_env)
    bill = billed_env / "bill.json"
    bill.write_text(BILL, encoding="utf-8")
    providers = args[args.index("--providers") + 1]

    result = cli_runner.invoke(
        app,
        [
            "batch",
            "costs",
            "--batch-id",
            BATCH,
            "--bill",
            str(bill),
            "--account",
            "fake",
            "--providers",
            providers,
        ],
    )

    assert result.exit_code == 2, result.output
    assert "does not support billing import" in _flat(result.output)
    # Nothing was imported: only the two platform-recorded entries remain.
    assert {row[2] for row in _bill_entries(billed_env)} == {"gateway", "endpoint-manager"}


def test_bill_and_account_are_required_together(
    cli_runner: CliRunner, args: list[str], billed_env: Path
) -> None:
    bill = billed_env / "bill.json"
    bill.write_text(BILL, encoding="utf-8")
    providers = args[args.index("--providers") + 1]

    bill_only = cli_runner.invoke(
        app,
        [
            "batch",
            "costs",
            "--batch-id",
            BATCH,
            "--bill",
            str(bill),
            "--providers",
            providers,
        ],
    )
    assert bill_only.exit_code == 2
    assert "go together" in _flat(bill_only.output)
    account_only = cli_runner.invoke(
        app,
        [
            "batch",
            "costs",
            "--batch-id",
            BATCH,
            "--account",
            "novita",
            "--providers",
            providers,
        ],
    )
    assert account_only.exit_code == 2
    assert "go together" in _flat(account_only.output)


def test_unknown_account_is_a_usage_error(
    cli_runner: CliRunner, args: list[str], billed_env: Path
) -> None:
    bill = billed_env / "bill.json"
    bill.write_text(BILL, encoding="utf-8")
    providers = args[args.index("--providers") + 1]

    result = cli_runner.invoke(
        app,
        [
            "batch",
            "costs",
            "--batch-id",
            BATCH,
            "--bill",
            str(bill),
            "--account",
            "nope",
            "--providers",
            providers,
        ],
    )

    assert result.exit_code == 2
    assert "no model account 'nope'" in _flat(result.output)


def test_novita_bill_option_is_gone(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    result = cli_runner.invoke(
        app, ["batch", "costs", "--batch-id", BATCH, "--novita-bill", "x.json"]
    )
    assert result.exit_code == 2
    assert "No such option" in result.output
