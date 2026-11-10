"""Shared plumbing of the spleef CLI commands: settings builders, output helpers, exit mapping.

Split out of :mod:`evalspleef.control.cli.app` so command modules (``judges``,
``batch_checks``) can use the same store/config loading and table/JSON output
without importing the (circular) app module.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any

import typer

from evalspleef.control.config import ConfigError
from evalspleef.core.batch import BatchGuardFailed
from evalspleef.spi.errors import SpleefError

from .context import Settings
from .errors import EXIT_DOMAIN, EXIT_USAGE, CliError

__all__ = [
    "EVENTS_OPT",
    "INCLUDE_OPT",
    "JSON_OPT",
    "STATE_OPT",
    "STORE_OPT",
    "TASKS_OPT",
    "emit_json",
    "emit_table",
    "run_coro",
    "store_settings",
]

EVENTS_OPT = typer.Option(None, "--events-url", envvar="SPLEEF_EVENTS_URL", help="event store URL")
STORE_OPT = typer.Option(None, "--store-url", envvar="SPLEEF_STORE_URL", help="result store URL")
STATE_OPT = typer.Option(
    None, "--state-dir", envvar="SPLEEF_STATE_DIR", help="state directory (freeze records)"
)
TASKS_OPT = typer.Option(Path("tasks"), "--tasks-dir", help="task packs root directory")
JSON_OPT = typer.Option(False, "--json", help="print machine-readable JSON")
INCLUDE_OPT = typer.Option([], "--include", help="glob, default: every pack")


def store_settings(
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    providers: Path | None = None,
) -> Settings:
    """Settings for store commands; ``providers`` only where a command resolves accounts."""
    return Settings.from_options(
        models=Path("config/models.yaml"),
        providers=providers,
        batch=None,
        tasks_dir=Path("tasks"),
        templates_registry=None,
        store_url=store_url,
        events_url=events_url,
        state_dir=state_dir,
    )


def emit_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    text = [[str(cell) for cell in row] for row in rows]
    widths = [len(str(header)) for header in headers]
    for row in text:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(str(header).ljust(widths[index]) for index, header in enumerate(headers))
    typer.echo(line)
    typer.echo("  ".join("-" * width for width in widths))
    for row in text:
        typer.echo("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def emit_json(data: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def run_coro(coro: Coroutine[Any, Any, int]) -> None:
    """Run a command coroutine and map failures to the CLI exit codes."""
    try:
        code = asyncio.run(coro)
    except CliError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(exc.exit_code) from None
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from None
    except BatchGuardFailed as exc:
        typer.secho(f"batch guard failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_DOMAIN) from None
    except SpleefError as exc:
        typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_DOMAIN) from None
    raise typer.Exit(code)
