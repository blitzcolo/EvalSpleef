#!/usr/bin/env python3
"""logic-002 参考解 A：正向模拟（独立实现）。

思路：把折好的叠看成若干「列桶」，每列桶里放当前落在该列的原始格子编号。
每折一次，列数减半，按折叠方向把两列合并成一个桶；全部折完后，
打孔列桶里的全部格子就是答案，合并去重后升序输出。

只依赖标准库。用法：
    python3 reference-a.py <instances.json> <answer.json>
"""
from __future__ import annotations

import json
import sys

N = 32


def fold_once(buckets: list[list[int]], direction: str) -> list[list[int]]:
    width = len(buckets)
    half = width // 2
    result: list[list[int]] = []
    for j in range(half):
        if direction == "L":
            pair = buckets[half + j] + buckets[half - 1 - j]
        elif direction == "R":
            pair = buckets[j] + buckets[width - 1 - j]
        else:
            raise ValueError(f"未知方向 {direction}")
        result.append(sorted(pair))
    return result


def solve_one(folds: list[str], punches: list[int]) -> list[int]:
    buckets = [[i + 1] for i in range(N)]
    for direction in folds:
        buckets = fold_once(buckets, direction)
    width = len(buckets)
    if any(not (1 <= p <= width) for p in punches):
        raise ValueError("打孔列超出折后宽度")
    holes: set[int] = set()
    for p in punches:
        holes.update(buckets[p - 1])
    return sorted(holes)


def solve(doc: dict) -> dict:
    out = []
    for inst in doc["instances"]:
        folds, punches = inst["folds"], inst["punches"]
        holes = solve_one(folds, punches)
        width = N >> len(folds)
        derivation = (
            f"实例 {inst['id']}：从 32 个单格列出发逐次对折 {len(folds)} 次折成 {width} 列，"
            f"打孔列 {'、'.join(map(str, punches))} 每列含 {1 << len(folds)} 层，"
            f"展开合并得到 {len(holes)} 个格子并升序排列。"
        )
        out.append({"id": inst["id"], "holes": holes, "derivation": derivation})
    return {"instances": out}


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
