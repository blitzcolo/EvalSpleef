#!/usr/bin/env python3
"""data-001 隐藏数据生成器（验证侧；不进入 Agent 工作区）。

生成两类数据：
  * sample（Agent 可见）：workspace/access.log，seed=880001，约 45 行，覆盖 3 种格式 + 5 类脏行；
  * hidden（验证侧）：seed=771001，约 420 行，分布与样例不同（路径/客户端/状态码分布变化）。

脏行原因分类（题面与 oracle.py 共享口径）：
  bad_format / bad_timestamp / bad_status / bad_duration / duplicate_rid
"""
from __future__ import annotations

import argparse
import json
import random

CLIENTS = [
    "10.0.0.3", "10.0.0.7", "10.0.0.9", "10.0.1.2",
    "10.0.1.8", "172.16.0.4", "172.16.0.5", "192.168.1.20",
]
PATHS = [
    "/api/orders", "/api/users", "/api/cart", "/api/search",
    "/api/products", "/api/login", "/api/health",
]
METHODS = ["GET", "POST", "PUT", "DELETE"]
STATUSES = [200, 200, 201, 204, 302, 400, 404, 500, 502, 503]


def _ts(rng: random.Random) -> str:
    hour = rng.randrange(0, 24)
    minute = rng.randrange(0, 60)
    second = rng.randrange(0, 60)
    return f"2026-11-0{rng.randrange(3, 6)}T{hour:02d}:{minute:02d}:{second:02d}Z"


def _duration_ms(rng: random.Random) -> float:
    return round(rng.uniform(3, 950), 1)


def _fmt_duration(rng: random.Random, value_ms: float) -> str:
    if rng.random() < 0.3:
        return f"{value_ms / 1000:.3f}s"
    return f"{value_ms:.0f}ms"


def _valid_line(rng: random.Random, rid: str | None) -> tuple[str, str, str, str, int, float]:
    client = rng.choice(CLIENTS)
    path = rng.choice(PATHS)
    method = rng.choice(METHODS)
    status = rng.choice(STATUSES)
    dur = _duration_ms(rng)
    ts = _ts(rng)
    style = rng.randrange(3)
    rid_suffix = f" rid={rid}" if rid else ""
    if style == 0:  # 简单文本格式
        text = f"{ts} {client} {method} {path} {status} {_fmt_duration(rng, dur)}{rid_suffix}"
    elif style == 1:  # Apache 格式
        day = int(ts[8:10])
        hhmmss = ts[11:19]
        apache_ts = f"{day:02d}/Nov/2026:{hhmmss} +0000"
        text = (
            f'{client} - - [{apache_ts}] "{method} {path} HTTP/1.1" '
            f"{status} {_fmt_duration(rng, dur)}{rid_suffix}"
        )
    else:  # JSON 行
        obj = {
            "ts": ts,
            "client": client,
            "method": method,
            "path": path,
            "status": status,
            "dur_ms": round(dur, 1),
        }
        if rid:
            obj["rid"] = rid
        text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return text, ts, client, path, status, dur


def _make_dirty(kind: str, rng: random.Random, existing_rid: str) -> str:
    client = rng.choice(CLIENTS)
    path = rng.choice(PATHS)
    dur = _duration_ms(rng)
    if kind == "bad_format":
        choices = [
            "-- rotation marker --",
            "<<log truncated>>",
            '{"unterminated": ',
            "GARBAGE",
        ]
        return rng.choice(choices)
    if kind == "bad_timestamp":
        bad = f"2026-13-40T25:61:61Z"
        return f"{bad} {client} GET {path} 200 {dur:.0f}ms"
    if kind == "bad_status":
        return f"{_ts(rng)} {client} POST {path} 999 {dur:.0f}ms"
    if kind == "bad_duration":
        return f"{_ts(rng)} {client} GET {path} 200 fast"
    if kind == "duplicate_rid":
        return f"{_ts(rng)} {client} GET {path} 200 {dur:.0f}ms rid={existing_rid}"
    raise ValueError(kind)


DIRT_KINDS = ["bad_format", "bad_timestamp", "bad_status", "bad_duration", "duplicate_rid"]


def build_lines(seed: int, count: int, ensure_coverage: bool = True) -> list[str]:
    rng = random.Random(seed)
    lines: list[str] = []
    issued_rids: list[str] = []
    while len(lines) < count:
        roll = rng.random()
        if roll < 0.15 and issued_rids:
            kind = rng.choice(DIRT_KINDS)
            lines.append(_make_dirty(kind, rng, rng.choice(issued_rids)))
            continue
        rid = None
        if rng.random() < 0.3:
            rid = f"r-{rng.randrange(10000, 99999)}"
            if rid not in issued_rids:
                issued_rids.append(rid)
            else:
                rid = None
        text, _ts_v, _client, _path, _status, _dur = _valid_line(rng, rid)
        lines.append(text)
    if ensure_coverage:
        # 保证 5 类脏行与 3 种格式都出现，供样例自检与隐藏测试覆盖
        if not issued_rids:
            text, _ts_v, _client, _path, _status, _dur = _valid_line(rng, None)
            lines.append(text)
            rid = f"r-{rng.randrange(10000, 99999)}"
            issued_rids.append(rid)
            lines.append(f"{_ts(rng)} 10.0.0.3 GET /api/health 200 12ms rid={rid}")
        for kind in DIRT_KINDS:
            lines.append(_make_dirty(kind, rng, rng.choice(issued_rids)))
    return lines


def build_sample(seed: int = 880001, count: int = 45) -> list[str]:
    return build_lines(seed, count)


def build_hidden(seed: int = 771001, count: int = 420) -> list[str]:
    return build_lines(seed, count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data-001 日志生成器")
    parser.add_argument("--export-sample", metavar="PATH")
    parser.add_argument("--export-hidden", metavar="PATH")
    parser.add_argument("--seed", type=int, default=880001)
    parser.add_argument("--count", type=int, default=45)
    args = parser.parse_args(argv)
    if args.export_sample:
        with open(args.export_sample, "w", encoding="utf-8") as fh:
            fh.write("\n".join(build_sample(args.seed, args.count)) + "\n")
        print(f"sample exported -> {args.export_sample}")
    if args.export_hidden:
        with open(args.export_hidden, "w", encoding="utf-8") as fh:
            fh.write("\n".join(build_hidden()) + "\n")
        print(f"hidden exported -> {args.export_hidden}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
