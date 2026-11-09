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
    "cached_tokens, reasoning_tokens, latency_ms, status_code, cold_start_ms, cost_usd, error_code, "
    "ttft_ms, e2e_ms, tpot_ms, itl_mean_ms, itl_p95_ms, itl_max_ms, itl_count"
)

#: Usage columns added after the first schema, with their SQL type; added in place on open so
#: stores written before them keep working, and their old rows read back as NULL.
USAGE_TIMING_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ttft_ms", "REAL"),
    ("e2e_ms", "REAL"),
    ("tpot_ms", "REAL"),
    ("itl_mean_ms", "REAL"),
    ("itl_p95_ms", "REAL"),
    ("itl_max_ms", "REAL"),
    ("itl_count", "INTEGER"),
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
        record.error_code,
        record.ttft_ms,
        record.e2e_ms,
        record.tpot_ms,
        record.itl_mean_ms,
        record.itl_p95_ms,
        record.itl_max_ms,
        record.itl_count,
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
        error_code=row[13],
        ttft_ms=_optional_float(row[14]),
        e2e_ms=_optional_float(row[15]),
        tpot_ms=_optional_float(row[16]),
        itl_mean_ms=_optional_float(row[17]),
        itl_p95_ms=_optional_float(row[18]),
        itl_max_ms=_optional_float(row[19]),
        itl_count=None if row[20] is None else int(row[20]),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)
