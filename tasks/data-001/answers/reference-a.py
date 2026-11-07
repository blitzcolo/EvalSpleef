#!/usr/bin/env python3
# TODO(run-on-host): 本参考解依赖 pandas（评测机镜像预装）；当前开发机未安装 pandas，
# 因此本地隐藏测试只做 py_compile 校验，pandas 路径在镜像内跑。等价可跑实现见 reference-b.py。
"""data-001 参考解 A：pandas 向量化清洗（独立实现，思路与 B 不同）。

思路：把日志读成单列 DataFrame，用 str.extract 一次性抽出简文本与 Apache 两种
正则格式，JSON 行单独用 json_normalize 展平，三者 concat 成统一表；然后用布尔掩码
按优先级标记脏行，最后用 groupby/agg 与排序完成聚合。

用法：python3 reference-a.py <access.log> <report.json>
"""
from __future__ import annotations

import json
import sys
from math import ceil

import pandas as pd

METHODS = ("GET", "POST", "PUT", "DELETE")
REASONS = ("bad_format", "bad_timestamp", "bad_status", "bad_duration", "duplicate_rid")
SIMPLE_PATTERN = (
    r"^(?P<ts>\S+) (?P<client>\S+) (?P<method>\S+) (?P<path>\S+) "
    r"(?P<status>\S+) (?P<dur>\S+)(?: rid=(?P<rid>\S+))?$"
)
APACHE_PATTERN = (
    r'^(?P<client>\S+) - - \[(?P<ts>[^\]]+)\] "(?P<method>\S+) (?P<path>\S+) HTTP/1\.1" '
    r'(?P<status>\S+) (?P<dur>\S+)(?: rid=(?P<rid>\S+))?$'
)


def _normalize_frame(lines: list[str]) -> pd.DataFrame:
    df = pd.DataFrame({"raw": lines})
    is_json = df["raw"].str.startswith("{")
    simple = df.loc[~is_json, "raw"].str.extract(SIMPLE_PATTERN)
    simple = simple.dropna(subset=["ts"])
    apache = df.loc[~is_json, "raw"].str.extract(APACHE_PATTERN).dropna(subset=["ts"])
    json_rows = []
    for raw in df.loc[is_json, "raw"]:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "dur_ms" in obj:
            obj = dict(obj)
            obj["dur"] = obj.pop("dur_ms")
            json_rows.append(obj)
    json_df = pd.DataFrame(json_rows)
    if not json_df.empty:
        json_df["ts"] = json_df["ts"].astype(str).str.replace(r"Z$", "", regex=True)
    simple = simple.assign(ts=simple["ts"].str.replace(r"Z$", "", regex=True))
    apache = apache.assign(
        ts=apache["ts"].str.replace(
            r"^(\d{2})/(\w{3})/(\d{4}):(\d{2}:\d{2}:\d{2}) \+0000$",
            r"\3-\2-\1 \4",
            regex=True,
        )
    )
    frames = [simple, apache, json_df]
    table = pd.concat(frames, ignore_index=True)
    return table


def build_report(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    table = _normalize_frame(lines)
    table["ts_ok"] = pd.to_datetime(table["ts"], format="%Y-%m-%dT%H:%M:%S", errors="coerce").notna()
    table["status_num"] = pd.to_numeric(table["status"], errors="coerce")
    table["status_ok"] = table["status_num"].between(100, 599)
    table["dur_ms"] = pd.to_numeric(
        table["dur"].astype(str).str.replace("ms", "", regex=False).str.replace("s", "", regex=False),
        errors="coerce",
    )
    table.loc[table["dur"].astype(str).str.endswith("s"), "dur_ms"] *= 1000.0
    table["dur_ok"] = table["dur_ms"].notna() & (table["dur_ms"] >= 0)
    table["method_ok"] = table["method"].isin(METHODS)
    table["path_ok"] = table["path"].astype(str).str.startswith("/")
    table["client_ok"] = table["client"].astype(str).str.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}")
    base_ok = table["ts_ok"] & table["method_ok"] & table["path_ok"] & table["client_ok"]
    format_ok = table["raw"].str.startswith("{") | (
        table["raw"].str.match(SIMPLE_PATTERN) | table["raw"].str.match(APACHE_PATTERN)
    )
    bad_format = ~format_ok
    bad_ts = format_ok & ~bad_format & ~table["ts_ok"]
    bad_status = format_ok & ~bad_format & table["ts_ok"] & ~table["status_ok"]
    bad_dur = format_ok & ~bad_format & table["ts_ok"] & table["status_ok"] & ~table["dur_ok"]
    remaining = format_ok & ~(bad_format | bad_ts | bad_status | bad_dur)
    dup = remaining & table["rid"].notna() & table["rid"].duplicated(keep="first")
    valid = remaining & ~dup

    total = int(valid.sum())
    reasons = {
        "bad_format": int(bad_format.sum()),
        "bad_timestamp": int(bad_ts.sum()),
        "bad_status": int(bad_status.sum()),
        "bad_duration": int(bad_dur.sum()),
        "duplicate_rid": int(dup.sum()),
    }
    clients = int(table.loc[valid, "client"].nunique())
    errors = int((table.loc[valid, "status_num"] >= 500).sum())
    durations = table.loc[valid, "dur_ms"].sort_values().tolist()
    if total:
        rank = max(1, ceil(0.95 * total))
        p95 = round(float(durations[rank - 1]), 1)
        error_rate = round(errors / total, 4)
    else:
        p95 = 0.0
        error_rate = 0.0
    counts = table.loc[valid, "path"].value_counts()
    top = sorted(counts.items(), key=lambda item: (-int(item[1]), item[0]))[:5]
    return {
        "total_valid": total,
        "malformed": sum(reasons.values()),
        "malformed_by_reason": reasons,
        "unique_clients": clients,
        "error_rate": error_rate,
        "p95_duration_ms": p95,
        "top_paths": [{"path": str(path), "count": int(count)} for path, count in top],
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python3 reference-a.py <access.log> <report.json>", file=sys.stderr)
        return 2
    src, dst = argv
    report = build_report(src)
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
