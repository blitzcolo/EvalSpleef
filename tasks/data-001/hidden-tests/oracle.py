#!/usr/bin/env python3
"""data-001 期望报告计算器（验证侧的唯一口径来源）。

与题面第 3 节规则逐条对应：
  * 3 种可接受格式：simple / apache / json；字段：ts(UTC)、client、method、path、status、duration
  * 脏行分类顺序：bad_format → bad_timestamp → bad_status → bad_duration → duplicate_rid
  * 聚合：total_valid / malformed / malformed_by_reason / unique_clients /
          error_rate(保留 4 位小数) / p95_duration_ms(nearest-rank，保留 1 位小数) /
          top_paths(按次数降序、同次数按路径升序，取前 5)
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime

TS_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
IP_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
METHOD_RE = re.compile(r"^(GET|POST|PUT|DELETE)$")
DUR_RE = re.compile(r"^(\d+(?:\.\d+)?)(ms|s)$")
SIMPLE_RE = re.compile(
    r"^(?P<ts>\S+) (?P<client>\S+) (?P<method>\S+) (?P<path>\S+) "
    r"(?P<status>\S+) (?P<dur>\S+)(?: rid=(?P<rid>\S+))?$"
)
APACHE_RE = re.compile(
    r'^(?P<client>\S+) - - \[(?P<ts>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+) HTTP/1\.1" '
    r'(?P<status>\S+) (?P<dur>\S+)(?: rid=(?P<rid>\S+))?$'
)
REASONS = ["bad_format", "bad_timestamp", "bad_status", "bad_duration", "duplicate_rid"]


class Parsed:
    __slots__ = ("ts", "client", "method", "path", "status", "dur_ms", "rid")

    def __init__(self, ts, client, method, path, status, dur_ms, rid):
        self.ts = ts
        self.client = client
        self.method = method
        self.path = path
        self.status = status
        self.dur_ms = dur_ms
        self.rid = rid


def parse_ts(text: str) -> datetime:
    if not TS_RE.match(text):
        raise ValueError("bad ts")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def parse_apache_ts(text: str) -> datetime:
    return datetime.strptime(text, "%d/%b/%Y:%H:%M:%S %z")


def parse_status(text: object) -> int:
    if isinstance(text, bool):
        raise ValueError("bad status")
    if isinstance(text, int):
        value = text
    elif isinstance(text, str) and text.isdigit():
        value = int(text)
    else:
        raise ValueError("bad status")
    if not (100 <= value <= 599):
        raise ValueError("bad status")
    return value


def parse_duration(token: object) -> float:
    if isinstance(token, (int, float)) and not isinstance(token, bool):
        value = float(token)
    elif isinstance(token, str):
        m = DUR_RE.match(token)
        if not m:
            raise ValueError("bad duration")
        value = float(m.group(1))
        if m.group(2) == "s":
            value *= 1000.0
    else:
        raise ValueError("bad duration")
    if not math.isfinite(value) or value < 0:
        raise ValueError("bad duration")
    return value


def classify(line: str, seen_rids: set[str]) -> tuple[Parsed | None, str | None]:
    """返回 (parsed, None) 或 (None, 脏行原因)。"""
    stripped = line.strip()
    if not stripped:
        return None, "bad_format"
    obj = None
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            return None, "bad_format"
        if not isinstance(obj, dict):
            return None, "bad_format"
        required = {"ts", "client", "method", "path", "status", "dur_ms"}
        if not required <= set(obj):
            return None, "bad_format"
        try:
            ts = parse_ts(str(obj["ts"]))
        except (ValueError, TypeError):
            return None, "bad_timestamp"
        try:
            status = parse_status(obj["status"])
        except ValueError:
            return None, "bad_status"
        try:
            dur_ms = parse_duration(obj["dur_ms"])
        except ValueError:
            return None, "bad_duration"
        client = obj["client"]
        method = obj["method"]
        path = obj["path"]
        rid = obj.get("rid")
        if not (isinstance(client, str) and IP_RE.match(client)):
            return None, "bad_format"
        if not (isinstance(method, str) and METHOD_RE.match(method)):
            return None, "bad_format"
        if not (isinstance(path, str) and path.startswith("/")):
            return None, "bad_format"
        if rid is not None and not isinstance(rid, str):
            return None, "bad_format"
    else:
        m = SIMPLE_RE.match(stripped)
        if m:
            try:
                ts = parse_ts(m.group("ts"))
            except ValueError:
                return None, "bad_timestamp"
            client, method, path = m.group("client"), m.group("method"), m.group("path")
            if not (IP_RE.match(client) and METHOD_RE.match(method) and path.startswith("/")):
                return None, "bad_format"
            try:
                status = parse_status(m.group("status"))
            except ValueError:
                return None, "bad_status"
            try:
                dur_ms = parse_duration(m.group("dur"))
            except ValueError:
                return None, "bad_duration"
            rid = m.group("rid")
        else:
            m = APACHE_RE.match(stripped)
            if not m:
                return None, "bad_format"
            try:
                ts = parse_apache_ts(m.group("ts"))
            except ValueError:
                return None, "bad_timestamp"
            client, method, path = m.group("client"), m.group("method"), m.group("path")
            if not (IP_RE.match(client) and METHOD_RE.match(method) and path.startswith("/")):
                return None, "bad_format"
            try:
                status = parse_status(m.group("status"))
            except ValueError:
                return None, "bad_status"
            try:
                dur_ms = parse_duration(m.group("dur"))
            except ValueError:
                return None, "bad_duration"
            rid = m.group("rid")
    if rid is not None:
        if rid in seen_rids:
            return None, "duplicate_rid"
        seen_rids.add(rid)
    return Parsed(str(ts), client, method, path, status, dur_ms, rid), None


def build_report(lines: list[str]) -> dict:
    reasons = {r: 0 for r in REASONS}
    valid: list[Parsed] = []
    seen: set[str] = set()
    for line in lines:
        parsed, reason = classify(line, seen)
        if reason is not None:
            reasons[reason] += 1
        else:
            valid.append(parsed)
    total = len(valid)
    clients = {p.client for p in valid}
    errors = sum(1 for p in valid if p.status >= 500)
    durations = sorted(p.dur_ms for p in valid)
    if total:
        rank = max(1, math.ceil(0.95 * total))
        p95 = round(durations[rank - 1], 1)
        error_rate = round(errors / total, 4)
    else:
        p95 = 0.0
        error_rate = 0.0
    counts: dict[str, int] = {}
    for p in valid:
        counts[p.path] = counts.get(p.path, 0) + 1
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        "total_valid": total,
        "malformed": sum(reasons.values()),
        "malformed_by_reason": reasons,
        "unique_clients": len(clients),
        "error_rate": error_rate,
        "p95_duration_ms": p95,
        "top_paths": [{"path": p, "count": c} for p, c in top],
    }


def report_from_file(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    return build_report(lines)
