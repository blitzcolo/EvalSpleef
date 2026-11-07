#!/usr/bin/env python3
"""logic-001 答案比对脚本（验证侧；不进入 Agent 工作区）。

职责边界：
1. 只做结构与数值比对，输出机器可读的违规码（violation code）；
2. 不生成面向被测 Agent 的自然语言反馈（F1/F2/F3 文案由 defects.yaml 与
   persona.md 提供，避免判定口径泄露）；
3. 判定口径：格式违规 → 硬门槛失败；5 个实例的 rule 与 next 全部正确 → 硬门槛通过。

用法：
    python3 checker.py --answer answer.json
    python3 checker.py --answer answer.json --json    # 输出完整结构化结果
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

N = 6
PERMUTATIONS = {
    "id": (0, 1, 2, 3, 4, 5),
    "rev": (5, 4, 3, 2, 1, 0),
    "rot_l1": (1, 2, 3, 4, 5, 0),
    "rot_r1": (5, 0, 1, 2, 3, 4),
    "swap_halves": (3, 4, 5, 0, 1, 2),
    "rot_halves": (1, 2, 0, 4, 5, 3),
    "evens_first": (0, 2, 4, 1, 3, 5),
    "rev_halves": (2, 1, 0, 5, 4, 3),
}
A_VALUES = (1, 3, 7, 9)
MAX_DERIVATION_CHARS = 300
MAX_DERIVATION_SENTENCES = 3
FENCE = "```"

TOP_KEYS = {"instances"}
INSTANCE_KEYS = {"id", "rule", "next", "derivation"}
RULE_KEYS = {"perm", "a", "b"}


def apply_rule(rule: dict, board: list[int]) -> list[int]:
    perm = PERMUTATIONS[rule["perm"]]
    return [(rule["a"] * board[perm[i]] + rule["b"]) % 10 for i in range(N)]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _sentence_count(text: str) -> int:
    parts = [p for p in re.split(r"[。！？!?]+|\.(?=\s|$)", text.strip()) if p.strip()]
    return len(parts)


def load_answer(path: str) -> tuple[dict | None, list[dict]]:
    """读取并做最外层格式校验，返回 (data, violations)。"""
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


def _check_rule(rule: object) -> list[dict]:
    out: list[dict] = []
    if not isinstance(rule, dict):
        return [{"code": "RULE_NOT_OBJECT"}]
    if set(rule) != RULE_KEYS:
        out.append({"code": "RULE_KEYS", "detail": sorted(rule.keys())})
    perm = rule.get("perm")
    if not isinstance(perm, str) or perm not in PERMUTATIONS:
        out.append({"code": "PERM_UNKNOWN", "detail": repr(perm)})
    a = rule.get("a")
    if not _is_int(a) or a not in A_VALUES:
        out.append({"code": "A_OUT_OF_FAMILY", "detail": repr(a)})
    b = rule.get("b")
    if not _is_int(b) or not (0 <= b <= 9):
        out.append({"code": "B_OUT_OF_RANGE", "detail": repr(b)})
    return out


def _check_next(board: object) -> list[dict]:
    if not isinstance(board, list) or len(board) != N:
        return [{"code": "NEXT_SHAPE"}]
    if any(not _is_int(v) or not (0 <= v <= 9) for v in board):
        return [{"code": "NEXT_OUT_OF_RANGE"}]
    return []


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


def rule_is_consistent(rule: dict, boards: list[list[int]]) -> bool:
    for cur, nxt in zip(boards, boards[1:]):
        if apply_rule(rule, cur) != nxt:
            return False
    return True


def score(answer_path: str, expected_instances: list[dict]) -> dict:
    """对一份 answer.json 打分，返回结构化结果（机器码，无自然语言判定话术）。"""
    data, violations = load_answer(answer_path)
    result: dict = {
        "task_id": "logic-001",
        "answer_path": answer_path,
        "format_violations": violations,
        "instances": [],
        "all_rules_correct": False,
        "all_next_correct": False,
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
        item_violations += _check_rule(item.get("rule"))
        item_violations += _check_next(item.get("next"))
        item_violations += _check_derivation(item.get("derivation"))
        rule = item.get("rule")
        next_board = item.get("next")
        rule_ok = False
        next_ok = False
        self_consistent = False
        if isinstance(rule, dict) and not _check_rule(rule):
            rule_ok = rule == expected["rule"]
            self_consistent = rule_is_consistent(rule, expected["boards"])
            next_ok = next_board == expected["next"]
        if not rule_ok:
            item_violations.append({"code": "RULE_MISMATCH"})
        if not next_ok:
            item_violations.append({"code": "NEXT_MISMATCH"})
        if isinstance(rule, dict) and not _check_rule(rule) and not self_consistent:
            item_violations.append({"code": "RULE_NOT_SELF_CONSISTENT"})
        result["instances"].append(
            {
                "instance_id": iid,
                "rule_ok": rule_ok,
                "next_ok": next_ok,
                "rule_self_consistent": self_consistent,
                "violations": item_violations,
            }
        )
        violations.extend({"instance_id": iid, **v} for v in item_violations)

    missing = sorted(set(expected_by_id) - seen)
    if missing:
        violations.append({"code": "INSTANCE_MISSING", "detail": missing})

    result["format_violations"] = [
        v for v in violations if v["code"] not in ("RULE_MISMATCH", "NEXT_MISMATCH")
    ]
    result["all_rules_correct"] = all(x["rule_ok"] for x in result["instances"]) and not missing
    result["all_next_correct"] = all(x["next_ok"] for x in result["instances"]) and not missing
    result["hard_gate_passed"] = (
        not result["format_violations"]
        and result["all_rules_correct"]
        and result["all_next_correct"]
    )
    result["tier_hint"] = "P" if result["hard_gate_passed"] else "F"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="logic-001 答案比对")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--expected-seed", type=int, default=20261106)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import generator  # 验证侧模块：生成器与期望答案

    expected = generator.build_instances(args.expected_seed)
    result = score(args.answer, expected)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if result["hard_gate_passed"] else "FAIL"
        print(f"{status} hard_gate_passed={result['hard_gate_passed']}")
        for v in result["format_violations"]:
            print(f"  violation {v}")
        print(f"  rules_ok={result['all_rules_correct']} next_ok={result['all_next_correct']}")
    return 0 if result["hard_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
