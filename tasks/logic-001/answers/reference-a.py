#!/usr/bin/env python3
"""logic-001 参考解 A：整族穷举（独立的枚举搜索实现）。

思路：把规则族 8 种置换 x 4 个 a x 10 个 b = 320 条候选全部代入记录，
筛出唯一幸存者，再用它推出下一棋盘。

只依赖标准库。用法：
    python3 reference-a.py <instances.json> <answer.json>
"""
from __future__ import annotations

import json
import sys

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


def transform(perm_name: str, a: int, b: int, board: list[int]) -> list[int]:
    perm = PERMUTATIONS[perm_name]
    return [(a * board[perm[i]] + b) % 10 for i in range(N)]


def fits(perm_name: str, a: int, b: int, boards: list[list[int]]) -> bool:
    for cur, nxt in zip(boards, boards[1:]):
        if transform(perm_name, a, b, cur) != nxt:
            return False
    return True


def solve_one(boards: list[list[int]]) -> dict:
    survivors = []
    for perm_name in PERMUTATIONS:
        for a in A_VALUES:
            for b in range(10):
                if fits(perm_name, a, b, boards):
                    survivors.append({"perm": perm_name, "a": a, "b": b})
    if len(survivors) != 1:
        raise ValueError(f"规则族内一致规则数为 {len(survivors)}，记录不满足唯一解前提")
    rule = survivors[0]
    return rule


def solve(doc: dict) -> dict:
    out_instances = []
    for inst in doc["instances"]:
        boards = inst["boards"]
        rule = solve_one(boards)
        nxt = transform(rule["perm"], rule["a"], rule["b"], boards[-1])
        derivation = (
            f"实例 {inst['id']}：把 {len(boards) - 1} 次转移代入规则族的 320 条候选，"
            f"唯一一致者为 perm={rule['perm']}、a={rule['a']}、b={rule['b']}；"
            f"据此下一棋盘唯一确定。"
        )
        out_instances.append(
            {
                "id": inst["id"],
                "rule": rule,
                "next": nxt,
                "derivation": derivation,
            }
        )
    return {"instances": out_instances}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python3 reference-a.py <instances.json> <answer.json>", file=sys.stderr)
        return 2
    src, dst = argv
    with open(src, encoding="utf-8") as fh:
        doc = json.load(fh)
    answer = solve(doc)
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(answer, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
