#!/usr/bin/env python3
"""logic-002 答案比对脚本（验证侧；不进入 Agent 工作区）。

职责边界：
1. 只做结构与数值比对，输出机器可读违规码；
2. 不生成面向被测 Agent 的自然语言反馈（F1/F2/F3 文案在 defects.yaml/persona.md）；
3. 判定口径：格式违规 → 硬门槛失败；4 个实例的 holes 全部正确 → 硬门槛通过。

用法：
    python3 checker.py --answer answer.json
    python3 checker.py --answer answer.json --json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

N = 32
MAX_DERIVATION_CHARS = 300
MAX_DERIVATION_SENTENCES = 3
FENCE = "```"

TOP_KEYS = {"instances"}
INSTANCE_KEYS = {"id", "holes", "derivation"}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sentence_count(text: str) -> int:
    parts = [p for p in re.split(r"[。！？!?]+|\.(?=\s|$)", text.strip()) if p.strip()]
    return len(parts)


def load_answer(path: str) -> tuple[dict | None, list[dict]]:
    violations: list[dict] = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None, [{"code": "ANSWER_MISSING", "detail": path}]
    stripped = text.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        violations.append({"code": "NOT_PURE_JSON"})
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        violations.append({"code": "JSON_INVALID", "detail": str(exc)[:120]})
        return None, violations
    if not isinstance(data, dict):
        violations.append({"code": "TOP_LEVEL_NOT_OBJECT"})
        return None, violations
    if set(data) != TOP_KEYS:
        violations.append({"code": "TOP_LEVEL_KEYS", "detail": sorted(data.keys())})
        return None, violations
    if not isinstance(data.get("instances"), list):
        violations.append({"code": "INSTANCES_NOT_LIST"})
        return None, violations
    return data, violations


def _check_holes(holes: object) -> list[dict]:
    out: list[dict] = []
    if not isinstance(holes, list):
        return [{"code": "HOLES_NOT_LIST"}]
    if len(holes) > N:
        out.append({"code": "HOLES_TOO_MANY", "detail": len(holes)})
    if any(not _is_int(v) or not (1 <= v <= N) for v in holes):
        return out + [{"code": "HOLES_OUT_OF_RANGE"}]
    if len(set(holes)) != len(holes):
        out.append({"code": "HOLES_DUPLICATED"})
    if holes != sorted(holes):
        out.append({"code": "HOLES_UNSORTED"})
    return out


def _check_derivation(text: object) -> list[dict]:
    out: list[dict] = []
    if not isinstance(text, str):
        return [{"code": "DERIVATION_MISSING"}]
    norm = unicodedata.normalize("NFC", text).strip()
    if not norm:
        out.append({"code": "DERIVATION_EMPTY"})
    if len(norm) > MAX_DERIVATION_CHARS:
        out.append({"code": "DERIVATION_TOO_LONG", "detail": len(norm)})
    if FENCE in norm:
        out.append({"code": "DERIVATION_HAS_CODE_FENCE"})
    if _sentence_count(norm) > MAX_DERIVATION_SENTENCES:
        out.append({"code": "DERIVATION_TOO_MANY_SENTENCES", "detail": _sentence_count(norm)})
    return out


def score(answer_path: str, expected_instances: list[dict]) -> dict:
    data, violations = load_answer(answer_path)
    result: dict = {
        "task_id": "logic-002",
        "answer_path": answer_path,
        "format_violations": violations,
        "instances": [],
        "all_holes_correct": False,
        "hard_gate_passed": False,
    }
    if data is None:
        return result

    expected_by_id = {inst["id"]: inst for inst in expected_instances}
    seen: set[int] = set()
    for item in data["instances"]:
        if not isinstance(item, dict):
            violations.append({"code": "INSTANCE_NOT_OBJECT"})
            continue
        iid = item.get("id")
        if not _is_int(iid) or iid not in expected_by_id:
            violations.append({"code": "INSTANCE_ID_UNKNOWN", "detail": repr(iid)})
            continue
        if iid in seen:
            violations.append({"code": "INSTANCE_ID_DUPLICATED", "detail": iid})
            continue
        seen.add(iid)
        expected = expected_by_id[iid]
        item_violations: list[dict] = []
        if set(item.keys()) != INSTANCE_KEYS:
            item_violations.append({"code": "INSTANCE_KEYS", "detail": sorted(item.keys())})
        item_violations += _check_holes(item.get("holes"))
        item_violations += _check_derivation(item.get("derivation"))
        holes_ok = item.get("holes") == expected["holes"] and not _check_holes(item.get("holes"))
        if not holes_ok:
            item_violations.append({"code": "HOLES_MISMATCH"})
        result["instances"].append(
            {
                "instance_id": iid,
                "holes_ok": holes_ok,
                "violations": item_violations,
            }
        )
        violations.extend({"instance_id": iid, **v} for v in item_violations)

    missing = sorted(set(expected_by_id) - seen)
    if missing:
        violations.append({"code": "INSTANCE_MISSING", "detail": missing})

    result["format_violations"] = [v for v in violations if v["code"] != "HOLES_MISMATCH"]
    result["all_holes_correct"] = (
        all(x["holes_ok"] for x in result["instances"]) and not missing
    )
    result["hard_gate_passed"] = (
        not result["format_violations"] and result["all_holes_correct"]
    )
    result["tier_hint"] = "P" if result["hard_gate_passed"] else "F"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="logic-002 答案比对")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--expected-seed", type=int, default=20261106)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import generator

    expected = generator.build_instances(args.expected_seed)
    result = score(args.answer, expected)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if result["hard_gate_passed"] else "FAIL"
        print(f"{status} hard_gate_passed={result['hard_gate_passed']}")
        for v in result["format_violations"]:
            print(f"  violation {v}")
        print(f"  holes_ok={result['all_holes_correct']}")
    return 0 if result["hard_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
