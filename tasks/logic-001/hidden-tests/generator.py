#!/usr/bin/env python3
"""logic-001 参数化生成器（验证侧；不进入 Agent 工作区）。

领域：6 格数字棋盘。隐藏规则 R = (perm, a, b)：
    next[i] = (a * cur[perm[i]] + b) mod 10
其中 perm 为表 PERMUTATIONS 中的一种位置置换，a ∈ {1,3,7,9}，b ∈ 0..9。
对局记录只展示连续棋盘，不展示规则；任务是归纳规则并预测下一棋盘。

生成不变量（由本文件的校验函数与隐藏测试共同断言）：
  G1 确定性：同一 seed 生成同一批实例。
  G2 最小性：展示 m 次转移；仅看前 m-1 次时一致规则 > 1 条。
  G3 唯一性：规则族（8*4*10=320 条）中恰好 1 条规则解释全部记录；
     因此下一棋盘唯一。
  G4 难度分布：m ∈ {2,3}，且批内 perm 互不相同。
"""
from __future__ import annotations

import argparse
import json
import random

N = 6
A_VALUES = (1, 3, 7, 9)
B_VALUES = tuple(range(10))

# 位置置换：next[i] = f(cur[PERMUTATIONS[name][i]])
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
FAMILY_SIZE = len(PERMUTATIONS) * len(A_VALUES) * len(B_VALUES)
PERM_NAMES = tuple(PERMUTATIONS)
HIDDEN_SEED = 20261106
VISIBLE_COUNT = 5
MAX_STEPS_TRIED = 3  # 展示的转移数 m 只取 2 或 3


def apply_rule(rule: dict, board: list[int]) -> list[int]:
    a, b = rule["a"], rule["b"]
    perm = PERMUTATIONS[rule["perm"]]
    return [(a * board[perm[i]] + b) % 10 for i in range(N)]


def all_rules():
    for perm in PERMUTATIONS:
        for a in A_VALUES:
            for b in B_VALUES:
                yield {"perm": perm, "a": a, "b": b}


def rule_key(rule: dict) -> tuple[str, int, int]:
    return (rule["perm"], rule["a"], rule["b"])


def consistent_rules(boards: list[list[int]]) -> list[dict]:
    """返回能解释 boards 中所有相邻转移的候选规则（规则族内）。"""
    out = []
    for rule in all_rules():
        ok = True
        for cur, nxt in zip(boards, boards[1:]):
            if apply_rule(rule, cur) != nxt:
                ok = False
                break
        if ok:
            out.append(rule)
    return out


def _random_rule(rng: random.Random) -> dict:
    perm = rng.choice(PERM_NAMES)
    a = rng.choice(A_VALUES)
    b = rng.choice(B_VALUES)
    return {"perm": perm, "a": a, "b": b}


def _make_candidate(rng: random.Random):
    """构造展示次数最少、且唯一确定规则的实例。

    先尝试 m=2；仅当 2 次转移仍不唯一时才保留更长的记录（m=3）。
    """
    rule = _random_rule(rng)
    if rule["perm"] == "id" and rule["a"] == 1 and rule["b"] == 0:
        return None  # 恒等规则平凡，排除
    boards = [[rng.randrange(10) for _ in range(N)]]
    for _ in range(MAX_STEPS_TRIED + 2):
        boards.append(apply_rule(rule, boards[-1]))
    for m in range(2, MAX_STEPS_TRIED + 1):
        shown = boards[: m + 1]
        hyps = consistent_rules(shown)
        if len(hyps) != 1 or rule_key(hyps[0]) != rule_key(rule):
            continue
        if m > 2 and len(consistent_rules(shown[:-1])) <= 1:
            continue  # 少看一次转移也已唯一，记录不是最小充分集
        nxt = apply_rule(rule, shown[-1])
        if nxt == shown[-1]:
            continue
        return {
            "rule": rule,
            "boards": shown,
            "next": nxt,
            "steps_shown": m,
        }
    return None


def build_instances(seed: int = HIDDEN_SEED, count: int = VISIBLE_COUNT) -> list[dict]:
    """确定性生成 count 个实例（含期望答案，供验证侧使用）。"""
    rng = random.Random(seed)
    instances: list[dict] = []
    used_perms: set[str] = set()
    guard = 0
    while len(instances) < count:
        guard += 1
        if guard > 500000:
            raise RuntimeError("生成器在预算内未找到足够实例，请检查参数")
        cand = _make_candidate(rng)
        if cand is None:
            continue
        if cand["rule"]["perm"] in used_perms and len(used_perms) < count:
            continue
        used_perms.add(cand["rule"]["perm"])
        instances.append(
            {
                "id": len(instances) + 1,
                "boards": cand["boards"],
                "rule": cand["rule"],
                "next": cand["next"],
                "steps_shown": cand["steps_shown"],
            }
        )
    return instances


def public_view(instances: list[dict]) -> dict:
    """去掉答案后的公开视图（写入 Agent 工作区）。"""
    return {
        "task": "logic-001",
        "family_version": "logic-rule-affine-v1",
        "board_len": N,
        "instances": [
            {"id": inst["id"], "boards": inst["boards"]} for inst in instances
        ],
    }


def validate(instances: list[dict], seed: int = HIDDEN_SEED) -> list[str]:
    """自检：唯一性、最小性、多样性、确定性。"""
    problems: list[str] = []
    if len(instances) != VISIBLE_COUNT:
        problems.append(f"实例数应为 {VISIBLE_COUNT}，实际 {len(instances)}")
    perms = [inst["rule"]["perm"] for inst in instances]
    if len(set(perms)) != len(perms):
        problems.append(f"批内 perm 重复：{perms}")
    for inst in instances:
        boards = inst["boards"]
        m = len(boards) - 1
        if m not in (2, 3):
            problems.append(f"实例 {inst['id']} 展示转移数 m={m} 不在 {{2,3}}")
        hyps = consistent_rules(boards)
        if len(hyps) != 1:
            problems.append(f"实例 {inst['id']} 一致规则数 {len(hyps)} != 1")
            continue
        if rule_key(hyps[0]) != rule_key(inst["rule"]):
            problems.append(f"实例 {inst['id']} 唯一规则与真实规则不符")
        if m > 2 and len(consistent_rules(boards[:-1])) <= 1:
            problems.append(f"实例 {inst['id']} 记录不是最小充分集（前 m-1 次已唯一）")
        if apply_rule(hyps[0], boards[-1]) != inst["next"]:
            problems.append(f"实例 {inst['id']} 期望下一棋盘与唯一规则不一致")
        if inst["next"] == boards[-1]:
            problems.append(f"实例 {inst['id']} 下一棋盘与当前相同（平凡）")
    again = build_instances(seed, VISIBLE_COUNT)
    if json.dumps(again, sort_keys=True) != json.dumps(instances, sort_keys=True):
        problems.append("生成器不确定：同一 seed 两次结果不同")
    return problems


def export_visible(path: str, seed: int = HIDDEN_SEED) -> None:
    instances = build_instances(seed)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(public_view(instances), fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="logic-001 生成器")
    parser.add_argument("--export", metavar="PATH", help="导出 Agent 可见实例")
    parser.add_argument("--seed", type=int, default=HIDDEN_SEED)
    parser.add_argument("--validate", action="store_true", help="自检生成不变量")
    args = parser.parse_args(argv)
    if args.export:
        export_visible(args.export, args.seed)
        print(f"exported -> {args.export}")
    if args.validate or not args.export:
        problems = validate(build_instances(args.seed), args.seed)
        if problems:
            for p in problems:
                print(f"[FAIL] {p}")
            return 1
        print(f"[OK] 生成器自检通过：{FAMILY_SIZE} 条规则族，{VISIBLE_COUNT} 个实例")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
