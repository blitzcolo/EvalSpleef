#!/usr/bin/env python3
"""data-001 参考解 B：纯 Python 流式清洗（可本地直接运行，仅标准库）。

思路：逐行识别格式（JSON / 简文本 / Apache），按固定优先级分类脏行，
用 Counter 做聚合；p95 用 nearest-rank（排序后取第 ceil(0.95*n) 个）。

用法：
    python3 reference-b.py <access.log> <report.json>
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime
from math import ceil

REASONS = ("bad_format", "bad_timestamp", "bad_status", "bad_duration", "duplicate_rid")
METHODS = {"GET", "POST", "PUT", "DELETE"}


def _strip_rid(tokens: list[str]) -> tuple[list[str], str | None]:
    if tokens and tokens[-1].startswith("rid="):
        return tokens[:-1], tokens[-1][4:]
    return tokens, None


def _valid_ip(text: str) -> bool:
    parts = text.split(".")
    return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def _duration_ms(token: str) -> float:
    if token.endswith("ms"):
        return float(token[:-2])
    if token.endswith("s"):
        return float(token[:-1]) * 1000.0
    raise ValueError("duration")


def _status(token: str | int) -> int:
    value = int(token)
    if not (100 <= value <= 599):
        raise ValueError("status")
    return value


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def _apache_ts(text: str) -> datetime:
    return datetime.strptime(text, "%d/%b/%Y:%H:%M:%S %z")


def classify(line: str) -> tuple[str, object] | None:
    """返回 (reason, None) 或 ('ok', dict)。"""
    raw = line.strip()
    if not raw:
        return ("bad_format", None)
    record: dict | None = None
    # ---------- JSON ----------
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return ("bad_format", None)
        if not isinstance(obj, dict) or not {"ts", "client", "method", "path", "status", "dur_ms"} <= set(obj):
            return ("bad_format", None)
        try:
            ts = _ts(str(obj["ts"]))
        except ValueError:
            return ("bad_timestamp", None)
        try:
            status = _status(obj["status"]) if not isinstance(obj["status"], bool) else _status(-1)
        except (ValueError, TypeError):
            return ("bad_status", None)
        dur = obj["dur_ms"]
        if isinstance(dur, bool) or not isinstance(dur, (int, float, str)):
            return ("bad_duration", None)
        try:
            dur_ms = float(dur) if not isinstance(dur, str) else _duration_ms(dur)
        except ValueError:
            return ("bad_duration", None)
        client, method, path = obj["client"], obj["method"], obj["path"]
        if not (isinstance(client, str) and _valid_ip(client)):
            return ("bad_format", None)
        if not (isinstance(method, str) and method in METHODS and isinstance(path, str) and path.startswith("/")):
            return ("bad_format", None)
        rid = obj.get("rid")
        if rid is not None and not isinstance(rid, str):
            return ("bad_format", None)
        record = {"ts": ts, "client": client, "method": method, "path": path,
                  "status": status, "dur_ms": dur_ms, "rid": rid}
    # ---------- 简文本 ----------
    elif raw.count(" ") == 5 or (raw.count(" ") == 6 and " rid=" in raw):
        tokens, rid = _strip_rid(raw.split(" "))
        if len(tokens) == 6:
            ts_txt, client, method, path, status_txt, dur_txt = tokens
            try:
                ts = _ts(ts_txt)
            except ValueError:
                return ("bad_timestamp", None)
            if not (_valid_ip(client) and method in METHODS and path.startswith("/")):
                return ("bad_format", None)
            try:
                status = _status(status_txt)
            except ValueError:
                return ("bad_status", None)
            try:
                dur_ms = _duration_ms(dur_txt)
            except ValueError:
                return ("bad_duration", None)
            record = {"ts": ts, "client": client, "method": method, "path": path,
                      "status": status, "dur_ms": dur_ms, "rid": rid}
    # ---------- Apache ----------
    if record is None:
        if ' - - [' not in raw or ' HTTP/1.1"' not in raw:
            return ("bad_format", None)
        head, tail = raw.split(' - - [', 1)
        ts_txt, tail = tail.split('] "', 1)
        try:
            ts = _apache_ts(ts_txt)
        except ValueError:
            return ("bad_timestamp", None)
        request, tail = tail.split('" ', 1)
        fields = tail.split(" ")
        if len(fields) == 2:
            status_txt, dur_txt = fields
        elif len(fields) == 3 and fields[2].startswith("rid="):
            status_txt, dur_txt = fields[0], fields[1]
        else:
            return ("bad_format", None)
        try:
            method, path, protocol = request.split(" ")
        except ValueError:
            return ("bad_format", None)
        if protocol != "HTTP/1.1" or method not in METHODS or not path.startswith("/"):
            return ("bad_format", None)
        if not _valid_ip(head):
            return ("bad_format", None)
        try:
            status = _status(status_txt)
        except ValueError:
            return ("bad_status", None)
        try:
            dur_ms = _duration_ms(dur_txt)
        except ValueError:
            return ("bad_duration", None)
        rid = fields[2][4:] if len(fields) == 3 else None
        record = {"ts": ts, "client": head, "method": method, "path": path,
                  "status": status, "dur_ms": dur_ms, "rid": rid}
    return ("ok", record)


def build_report(lines: list[str]) -> dict:
    reasons = {r: 0 for r in REASONS}
    statuses: Counter = Counter()
    clients: set[str] = set()
    paths: Counter = Counter()
    durations: list[float] = []
    seen_rids: set[str] = set()
    total = 0
    for line in lines:
        outcome = classify(line)
        if outcome is None:
            reasons["bad_format"] += 1
            continue
        kind, record = outcome
        if kind != "ok":
            reasons[kind] += 1
            continue
        rid = record["rid"]
        if rid is not None:
            if rid in seen_rids:
                reasons["duplicate_rid"] += 1
                continue
            seen_rids.add(rid)
        total += 1
        statuses[record["status"]] += 1
        clients.add(record["client"])
        paths[record["path"]] += 1
        durations.append(record["dur_ms"])
    durations.sort()
    errors = sum(count for status, count in statuses.items() if status >= 500)
    if total:
        rank = max(1, ceil(0.95 * total))
        p95 = round(durations[rank - 1], 1)
        error_rate = round(errors / total, 4)
    else:
        p95 = 0.0
        error_rate = 0.0
    top = sorted(paths.items(), key=lambda item: (-item[1], item[0]))[:5]
    return {
        "total_valid": total,
        "malformed": sum(reasons.values()),
        "malformed_by_reason": reasons,
        "unique_clients": len(clients),
        "error_rate": error_rate,
        "p95_duration_ms": p95,
        "top_paths": [{"path": path, "count": count} for path, count in top],
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python3 reference-b.py <access.log> <report.json>", file=sys.stderr)
        return 2
    src, dst = argv
    with open(src, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    report = build_report(lines)
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
