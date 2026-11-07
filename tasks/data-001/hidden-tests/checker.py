#!/usr/bin/env python3
"""data-001 报价报告比对脚本（验证侧；不进入 Agent 工作区）。

容差（题面第 5 节）：
  * 计数类字段（total_valid/malformed/unique_clients/malformed_by_reason/top_paths）精确相等；
  * error_rate 绝对容差 1e-4；
  * p95_duration_ms 绝对容差 1.0 ms。
格式违规 → 硬门槛失败；数值全部在容差内 → 硬门槛通过。

用法：python3 checker.py --answer report.json [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REASONS = ("bad_format", "bad_timestamp", "bad_status", "bad_duration", "duplicate_rid")
TOP_KEYS = {
    "total_valid",
    "malformed",
    "malformed_by_reason",
    "unique_clients",
    "error_rate",
    "p95_duration_ms",
    "top_paths",
}
ERROR_RATE_TOL = 1e-4
P95_TOL_MS = 1.0


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_report(path: str) -> tuple[dict | None, list[dict]]:
    violations: list[dict] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None, [{"code": "REPORT_MISSING", "detail": path}]
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        violations.append({"code": "NOT_PURE_JSON"})
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        return None, violations + [{"code": "JSON_INVALID", "detail": str(exc)[:120]}]
    if not isinstance(data, dict):
        return None, violations + [{"code": "TOP_LEVEL_NOT_OBJECT"}]
    if set(data) != TOP_KEYS:
        violations.append({"code": "TOP_LEVEL_KEYS", "detail": sorted(data.keys())})
        return None, violations
    return data, violations


def _check_shape(report: dict) -> list[dict]:
    out: list[dict] = []
    for key in ("total_valid", "malformed", "unique_clients"):
        if not _is_int(report.get(key)) or report[key] < 0:
            out.append({"code": "COUNT_TYPE", "detail": key})
    if not _is_number(report.get("error_rate")):
        out.append({"code": "NUMBER_TYPE", "detail": "error_rate"})
    if not _is_number(report.get("p95_duration_ms")):
        out.append({"code": "NUMBER_TYPE", "detail": "p95_duration_ms"})
    reasons = report.get("malformed_by_reason")
    if not isinstance(reasons, dict) or set(reasons) != set(REASONS):
        out.append({"code": "MALFORMED_REASONS_KEYS"})
    elif any(not _is_int(v) or v < 0 for v in reasons.values()):
        out.append({"code": "MALFORMED_REASONS_TYPE"})
    top = report.get("top_paths")
    if not isinstance(top, list) or len(top) > 5:
        out.append({"code": "TOP_PATHS_SHAPE"})
    else:
        for item in top:
            if not isinstance(item, dict) or set(item) != {"path", "count"}:
                out.append({"code": "TOP_PATHS_SHAPE"})
                break
            if not isinstance(item["path"], str) or not _is_int(item["count"]):
                out.append({"code": "TOP_PATHS_SHAPE"})
                break
    return out


def _top_paths_sorted(top: list[dict]) -> bool:
    keys = [(-item["count"], item["path"]) for item in top]
    return keys == sorted(keys)


def score(answer_path: str, expected: dict) -> dict:
    report, violations = load_report(answer_path)
    result: dict = {
        "task_id": "data-001",
        "answer_path": answer_path,
        "format_violations": violations,
        "value_mismatches": [],
        "all_values_correct": False,
        "hard_gate_passed": False,
        "tolerances": {"error_rate_abs": ERROR_RATE_TOL, "p95_ms_abs": P95_TOL_MS},
    }
    if report is None:
        return result
    violations.extend(_check_shape(report))
    top = report.get("top_paths")
    if isinstance(top, list) and all(
        isinstance(i, dict) and set(i) == {"path", "count"} and _is_int(i.get("count"))
        for i in top
    ):
        if not _top_paths_sorted(top):
            violations.append({"code": "TOP_PATHS_NOT_SORTED"})

    mismatches: list[dict] = []

    def require_equal(field: str, actual: object, wanted: object) -> None:
        if actual != wanted:
            mismatches.append({"field": field, "actual": actual, "expected": wanted})

    require_equal("total_valid", report.get("total_valid"), expected["total_valid"])
    require_equal("malformed", report.get("malformed"), expected["malformed"])
    require_equal("unique_clients", report.get("unique_clients"), expected["unique_clients"])
    require_equal(
        "malformed_by_reason", report.get("malformed_by_reason"), expected["malformed_by_reason"]
    )
    require_equal("top_paths", report.get("top_paths"), expected["top_paths"])

    error_rate = report.get("error_rate")
    if _is_number(error_rate):
        delta = abs(float(error_rate) - float(expected["error_rate"]))
        if delta > ERROR_RATE_TOL:
            mismatches.append(
                {"field": "error_rate", "actual": error_rate,
                 "expected": expected["error_rate"], "delta": round(delta, 6)}
            )
    p95 = report.get("p95_duration_ms")
    if _is_number(p95):
        delta = abs(float(p95) - float(expected["p95_duration_ms"]))
        if delta > P95_TOL_MS:
            mismatches.append(
                {"field": "p95_duration_ms", "actual": p95,
                 "expected": expected["p95_duration_ms"], "delta": round(delta, 4)}
            )

    result["format_violations"] = violations
    result["value_mismatches"] = mismatches
    result["all_values_correct"] = not mismatches
    result["hard_gate_passed"] = not violations and not mismatches
    result["tier_hint"] = "P" if result["hard_gate_passed"] else "F"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data-001 报告比对")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--data", help="输入日志路径（默认 workspace/access.log）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import oracle

    data_path = args.data or str(Path(__file__).resolve().parent.parent / "workspace" / "access.log")
    expected = oracle.report_from_file(data_path)
    result = score(args.answer, expected)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if result["hard_gate_passed"] else "FAIL"
        print(f"{status} hard_gate_passed={result['hard_gate_passed']}")
        for v in result["format_violations"]:
            print(f"  violation {v}")
        for m in result["value_mismatches"]:
            print(f"  mismatch {m}")
    return 0 if result["hard_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
