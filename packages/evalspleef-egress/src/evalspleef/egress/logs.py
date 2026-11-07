"""Append-only URL and DNS audit logs (PRD appendix D.3, invariant I16).

The proxy process writes these files outside the sandbox, one JSON object per line. Field
names and order are the contract; extra fields must not be added.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(clock: Clock) -> str:
    return clock().astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class UrlRecord:
    """One line of ``url.jsonl``."""

    ts: str
    trial_id: str
    round: int
    process: str
    method: str
    host: str
    path: str
    status: int
    bytes: int
    blocked_reason: str | None


@dataclass(frozen=True)
class DnsRecord:
    """One line of ``dns.jsonl``."""

    ts: str
    trial_id: str
    qname: str
    qtype: str
    answer: tuple[str, ...]
    blocked: bool


class AuditLog:
    """Writes ``url.jsonl`` and ``dns.jsonl`` under one directory.

    ``trial_id`` and ``round`` are per-listener settings fixed at construction time, so a
    proxy listening for one trial can never attribute a request to another trial.
    """

    def __init__(
        self,
        log_dir: str | Path,
        *,
        trial_id: str,
        round: int = 0,
        process: str = "unknown",
        clock: Clock = utc_now,
    ) -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._trial_id = trial_id
        self._round = round
        self._process = process
        self._clock = clock
        self._url_file = (self._dir / "url.jsonl").open("a", encoding="utf-8")
        self._dns_file = (self._dir / "dns.jsonl").open("a", encoding="utf-8")
        self._closed = False

    @property
    def url_path(self) -> Path:
        return self._dir / "url.jsonl"

    @property
    def dns_path(self) -> Path:
        return self._dir / "dns.jsonl"

    def write_url(
        self,
        *,
        method: str,
        host: str,
        path: str,
        status: int,
        bytes_count: int,
        blocked_reason: str | None,
    ) -> UrlRecord:
        record = UrlRecord(
            ts=_timestamp(self._clock),
            trial_id=self._trial_id,
            round=self._round,
            process=self._process,
            method=method,
            host=host,
            path=path,
            status=status,
            bytes=bytes_count,
            blocked_reason=blocked_reason,
        )
        self._write(self._url_file, record)
        return record

    def write_dns(
        self,
        *,
        qname: str,
        qtype: str,
        answer: Sequence[str],
        blocked: bool,
    ) -> DnsRecord:
        record = DnsRecord(
            ts=_timestamp(self._clock),
            trial_id=self._trial_id,
            qname=qname,
            qtype=qtype,
            answer=tuple(answer),
            blocked=blocked,
        )
        self._write(self._dns_file, record)
        return record

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._url_file.close()
        self._dns_file.close()

    @staticmethod
    def _write(file: TextIO, record: UrlRecord | DnsRecord) -> None:
        line = json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":"))
        # Both writers are only used from the proxy's event loop, so one synchronous
        # append per record keeps lines intact without a lock.
        file.write(line + "\n")
        file.flush()
