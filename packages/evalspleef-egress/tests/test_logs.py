from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from evalspleef.egress.logs import AuditLog

URL_FIELDS = [
    "ts",
    "trial_id",
    "round",
    "process",
    "method",
    "host",
    "path",
    "status",
    "bytes",
    "blocked_reason",
]
DNS_FIELDS = ["ts", "trial_id", "qname", "qtype", "answer", "blocked"]


def _read_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_url_and_dns_records_have_exactly_the_spec_fields(tmp_path: Path) -> None:
    fixed = datetime(2026, 1, 2, 3, 4, 5, 678000, tzinfo=UTC)
    audit = AuditLog(
        tmp_path,
        trial_id="trial-42",
        round=3,
        process="pip",
        clock=lambda: fixed,
    )
    audit.write_url(
        method="GET",
        host="pypi.org",
        path="/simple/requests/",
        status=200,
        bytes_count=1024,
        blocked_reason=None,
    )
    audit.write_dns(qname="pypi.org", qtype="A", answer=("203.0.113.7",), blocked=False)
    audit.close()

    url = _read_lines(tmp_path / "url.jsonl")
    assert len(url) == 1
    assert list(url[0]) == URL_FIELDS
    assert url[0] == {
        "ts": "2026-01-02T03:04:05.678Z",
        "trial_id": "trial-42",
        "round": 3,
        "process": "pip",
        "method": "GET",
        "host": "pypi.org",
        "path": "/simple/requests/",
        "status": 200,
        "bytes": 1024,
        "blocked_reason": None,
    }

    dns = _read_lines(tmp_path / "dns.jsonl")
    assert len(dns) == 1
    assert list(dns[0]) == DNS_FIELDS
    assert dns[0] == {
        "ts": "2026-01-02T03:04:05.678Z",
        "trial_id": "trial-42",
        "qname": "pypi.org",
        "qtype": "A",
        "answer": ["203.0.113.7"],
        "blocked": False,
    }


def test_writers_append_and_close_is_idempotent(tmp_path: Path) -> None:
    first = AuditLog(tmp_path, trial_id="t-1")
    first.write_url(
        method="GET", host="a.example", path="/", status=200, bytes_count=1, blocked_reason=None
    )
    first.close()
    first.close()

    second = AuditLog(tmp_path, trial_id="t-1")
    second.write_url(
        method="GET",
        host="b.example",
        path="/",
        status=403,
        bytes_count=0,
        blocked_reason="host_not_allowed",
    )
    second.close()

    lines = _read_lines(tmp_path / "url.jsonl")
    assert [line["host"] for line in lines] == ["a.example", "b.example"]
    assert lines[1]["blocked_reason"] == "host_not_allowed"
