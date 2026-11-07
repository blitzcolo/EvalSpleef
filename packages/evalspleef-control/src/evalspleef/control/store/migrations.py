"""Schema migrations for the relational store.

Migrations are plain SQL files in ``migrations/`` named ``NNNN_description.sql``,
applied in filename order inside a transaction and recorded in the
``schema_migration`` table. Only applied-once semantics are guaranteed; never
edit an applied migration, add a new one.

The SQL dialect is the portable subset shared by SQLite and PostgreSQL
(see :mod:`evalspleef.control.store.db`). Statements are split on semicolons
at end of line, so migration files must not contain semicolons inside string
literals or statement bodies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from .db import Database

__all__ = ["MIGRATIONS_DIR", "migrate"]

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _split_statements(script: str) -> list[str]:
    """Split a migration script into executable statements.

    Comment lines (``--``) are dropped and each statement must end with ``;``
    at the end of a line; this keeps the splitter trivial and deterministic.
    """
    statements: list[str] = []
    buffer: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buffer.append(line)
        if stripped.endswith(";"):
            statement = "\n".join(buffer).strip().removesuffix(";").strip()
            if statement:
                statements.append(statement)
            buffer = []
    if buffer:
        raise ValueError("migration script ends with an unterminated statement")
    return statements


async def migrate(db: Database) -> list[str]:
    """Apply pending migrations and return the names applied by this call.

    Idempotent: migrations already recorded in ``schema_migration`` are
    skipped, so calling :func:`migrate` on every startup is safe.
    """
    await db.execute(
        "CREATE TABLE IF NOT EXISTS schema_migration ("
        "name TEXT NOT NULL PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    rows = await db.fetch("SELECT name FROM schema_migration")
    applied = {str(row["name"]) for row in rows}
    pending = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql") if path.name not in applied)

    now = datetime.now(UTC).isoformat()
    for name in pending:
        script = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
        async with db.transaction():
            for statement in _split_statements(script):
                await db.execute(statement)
            await db.execute(
                "INSERT INTO schema_migration (name, applied_at) VALUES (%s, %s)", (name, now)
            )
    return pending
