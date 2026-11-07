"""Row codecs shared by the SQL stores."""

from __future__ import annotations

from typing import Any

from evalspleef.spi.gateway import UsageRecord

from . import StoredKey

KEY_COLUMNS = (
    "key_id, secret_sha256, trial_id, endpoint_id, max_budget_usd, max_tokens, "
    "expires_at, revoked, spent_cost_usd, spent_tokens"
)

USAGE_COLUMNS = (
    "ts, trial_id, round, endpoint_id, upstream_model, input_tokens, output_tokens, "
    "cached_tokens, reasoning_tokens, latency_ms, status_code, cold_start_ms, cost_usd"
)


def placeholders(style: str, count: int) -> str:
    """A ``count``-value placeholder list in sqlite (``?``) or postgres (``%s``) style."""
    if style == "qmark":
        return ", ".join("?" for _ in range(count))
    if style == "format":
        return ", ".join("%s" for _ in range(count))
    raise ValueError(f"unknown placeholder style: {style!r}")


def insert_statement(table: str, columns: str, style: str) -> str:
    return (
        f"INSERT INTO {table} ({columns}) VALUES ({placeholders(style, len(columns.split(',')))})"
    )


def key_params(key: StoredKey) -> tuple[Any, ...]:
    return (
        key.key_id,
        key.secret_sha256,
        key.trial_id,
        key.endpoint_id,
        key.max_budget_usd,
        key.max_tokens,
        key.expires_at,
        key.revoked,
        key.spent_cost_usd,
        key.spent_tokens,
    )


def key_from_row(row: Any) -> StoredKey:
    return StoredKey(
        key_id=row[0],
        secret_sha256=row[1],
        trial_id=row[2],
        endpoint_id=row[3],
        max_budget_usd=float(row[4]),
        max_tokens=int(row[5]),
        expires_at=row[6],
        revoked=bool(row[7]),
        spent_cost_usd=float(row[8]),
        spent_tokens=int(row[9]),
    )


def usage_params(record: UsageRecord) -> tuple[Any, ...]:
    return (
        record.ts,
        record.trial_id,
        record.round,
        record.endpoint_id,
        record.upstream_model,
        record.input_tokens,
        record.output_tokens,
        record.cached_tokens,
        record.reasoning_tokens,
        record.latency_ms,
        record.status_code,
        record.cold_start_ms,
        record.cost_usd,
    )


def usage_from_row(row: Any) -> UsageRecord:
    return UsageRecord(
        ts=row[0],
        trial_id=row[1],
        round=int(row[2]),
        endpoint_id=row[3],
        upstream_model=row[4],
        input_tokens=int(row[5]),
        output_tokens=int(row[6]),
        cached_tokens=int(row[7]),
        reasoning_tokens=int(row[8]),
        latency_ms=int(row[9]),
        status_code=int(row[10]),
        cold_start_ms=int(row[11]),
        cost_usd=float(row[12]),
    )
