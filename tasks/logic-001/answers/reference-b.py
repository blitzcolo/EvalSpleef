#!/usr/bin/env python3
"""logic-001 参考解 B：约束传播 / 同余方程求解（独立的代数实现）。

思路：不遍历 320 条规则族。对每个候选置换，把每对 (x, y) 看成同余式
    y ≡ a·x + b  (mod 10)
两两相减消去 b，得到 (x1 - x2)·a ≡ y1 - y2 (mod 10)，用扩展欧几里得解出
a 的候选集合，再交所有等式；最后由 b = y - a·x 回代，并用全部转移复核。

只依赖标准库。用法：
    python3 reference-b.py <instances.json> <answer.json>
"""
from __future__ import annotations

import json
import sys
from math import gcd

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


def solve_congruence(g: int, rhs: int, modulus: int = 10) -> set[int]:
    """解 g*a ≡ rhs (mod modulus)，返回 a 在 [0, modulus) 内的解集。"""
    g %= modulus
    rhs %= modulus
    if g == 0:
        return set(range(modulus)) if rhs == 0 else set()
    d = gcd(g, modulus)
    if rhs % d != 0:
        return set()
    g1, rhs1, m1 = g // d, rhs // d, modulus // d
    inv = pow(g1, -1, m1)
    first = (rhs1 * inv) % m1
    return {first + t * m1 for t in range(d)}


def candidate_a_values(pairs: list[tuple[int, int]]) -> set[int]:
    """由 (x, y) 位置对求满足 y = a*x + b (mod 10) 的 a 集合。"""
    possible = set(A_VALUES)
    x0, y0 = pairs[0]
    for x, y in pairs[1:]:
        solutions = solve_congruence(x - x0, y - y0)
        possible &= solutions
        if not possible:
            return set()
    return possible


def transform(perm_name: str, a: int, b: int, board: list[int]) -> list[int]:
    perm = PERMUTATIONS[perm_name]
    return [(a * board[perm[i]] + b) % 10 for i in range(N)]


def solve_one(boards: list[list[int]]) -> dict:
    survivors = []
    for perm_name in PERMUTATIONS:
        perm = PERMUTATIONS[perm_name]
        pairs: list[tuple[int, int]] = []
        for cur, nxt in zip(boards, boards[1:]):
            pairs.extend((cur[perm[i]], nxt[i]) for i in range(N))
        for a in candidate_a_values(pairs):
            # 用第一个位置对回代 b，然后只需验证其余位置对
            x0, y0 = pairs[0]
            b = (y0 - a * x0) % 10
            if all((a * x + b - y) % 10 == 0 for x, y in pairs):
                survivors.append({"perm": perm_name, "a": a, "b": b})
    if len(survivors) != 1:
        raise ValueError(f"约束传播得到 {len(survivors)} 条规则，记录不满足唯一解前提")
    return survivors[0]


def solve(doc: dict) -> dict:
    out_instances = []
    for inst in doc["instances"]:
        boards = inst["boards"]
        rule = solve_one(boards)
        nxt = transform(rule["perm"], rule["a"], rule["b"], boards[-1])
        derivation = (
            f"实例 {inst['id']}：对每个置换建立同余式 y≡a·x+b（mod 10），"
            f"两两相减解出 a={rule['a']}、回代得 b={rule['b']}，"
            f"唯一可行置换为 {rule['perm']}，故下一棋盘唯一。"
        )
        out_instances.append(
            {"id": inst["id"], "rule": rule, "next": nxt, "derivation": derivation}
        )
    return {"instances": out_instances}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print("usage: python3 reference-b.py <instances.json> <answer.json>", file=sys.stderr)
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
