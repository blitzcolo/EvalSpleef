#!/usr/bin/env python3
"""logic-002 参考解 B：逆向重建（独立实现）。

思路：不打孔，只问「折完后的第 c 列是从哪些原始格子来的」。从最终列号出发，
按折叠序列倒着展开：每倒退一次折叠，一列会变成折前的两列
（折叠方向决定这两列的位置），直到退回 32 个单格列；这些列号 +1 就是原始格子编号。

只依赖标准库。用法：
    python3 reference-b.py <instances.json> <answer.json>
"""
from __future__ import annotations

import json
import sys

N = 32


def ancestors(folds: list[str], c: int) -> set[int]:
    """返回折后第 c 列（0 始）由哪些原始列（0 始）组成。"""
    columns = {c}
    for idx in range(len(folds) - 1, -1, -1):
        before = N >> idx
        after = before // 2
        direction = folds[idx]
        nxt: set[int] = set()
        for col in columns:
            if not (0 <= col < after):
                raise ValueError(f"列号 {col} 超出第 {idx + 1} 折的宽度 {after}")
            if direction == "L":
                nxt.add(after + col)
                nxt.add(after - 1 - col)
            else:
                nxt.add(col)
                nxt.add(before - 1 - col)
        columns = nxt
    if len(columns) != 1 << len(folds):
        raise ValueError("逆向展开列数不符")
    return columns


def solve_one(folds: list[str], punches: list[int]) -> list[int]:
    width = N >> len(folds)
    if any(not (1 <= p <= width) for p in punches):
        raise ValueError("打孔列超出折后宽度")
    cols: set[int] = set()
    for p in punches:
        cols |= ancestors(folds, p - 1)
    return sorted(c + 1 for c in cols)


def solve(doc: dict) -> dict:
    out = []
    for inst in doc["instances"]:
        folds, punches = inst["folds"], inst["punches"]
        holes = solve_one(folds, punches)
        width = N >> len(folds)
        derivation = (
            f"实例 {inst['id']}：折后宽 {width} 列，从打孔列反推；"
            f"每倒退一次折叠一列拆成折前两列，共得到 {1 << len(folds)} 个原始列，"
            f"多为打孔处取并集，共 {len(holes)} 个格子并升序。"
        )
        out.append({"id": inst["id"], "holes": holes, "derivation": derivation})
    return {"instances": out}


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
