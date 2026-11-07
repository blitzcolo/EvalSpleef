#!/usr/bin/env python3
"""logic-002 参数化生成器与双求解器（验证侧；不进入 Agent 工作区）。

领域：一条 32 格纸条，格子按 1..32 编号。按给定方向序列反复「整叠对折」：
    L = 左半叠到右半上；R = 右半叠到左半上。
折完后在折好的叠上选若干列（1 始）打穿所有层，展开后求被打穿的格子编号（升序）。

双求解器（互为独立实现，隐藏测试断言二者一致）：
    solve_forward   —— 正向模拟：每列维护「当前在该列的原始格子集合」，逐折合并；
    solve_backward  —— 逆向重建：从最终列编号出发，按折痕反向展开成两个列编号。

生成不变量：
    G1 确定性：同一 seed 生成同一批实例；
    G2 每折后列数减半，最终宽度 = 32 / 2^m；
    G3 每个打孔列恰好穿透 2^m 层（互不为同一格子），答案大小 = 2^m × 打孔数；
    G4 正向与逆向求解器结果一致（保证题目无歧义）；
    G5 答案不是全部 32 个格子（非平凡），且批内折叠序列互不相同。
"""
from __future__ import annotations

import argparse
import json
import random

N = 32
SEED = 20261106
INSTANCE_SPECS = ((2, 1), (3, 1), (3, 2), (4, 1))  # (折叠次数 m, 打孔列数 k)
DIRECTIONS = ("L", "R")


# ---------------------------------------------------------------- 正向模拟
def solve_forward(folds: list[str], punches: list[int]) -> list[int]:
    """按列维护格子集合，逐次对折后合并。返回被打穿的原始格子编号（升序）。"""
    columns: list[list[int]] = [[i + 1] for i in range(N)]
    for direction in folds:
        width_before = len(columns)
        if width_before % 2 != 0:
            raise ValueError("纸条宽度必须保持偶数")
        half = width_before // 2
        merged: list[list[int]] = []
        for j in range(half):
            if direction == "L":
                group = columns[half + j] + columns[half - 1 - j]
            elif direction == "R":
                group = columns[j] + columns[width_before - 1 - j]
            else:
                raise ValueError(f"未知折叠方向：{direction}")
            merged.append(sorted(group))
        columns = merged
    width = len(columns)
    for p in punches:
        if not (1 <= p <= width):
            raise ValueError(f"打孔列 {p} 超出折后宽度 {width}")
    punched = set()
    for p in punches:
        punched.update(columns[p - 1])
    return sorted(punched)


# ---------------------------------------------------------------- 逆向重建
def _expand_columns(folds: list[str], final_columns: set[int]) -> set[int]:
    """从最终列编号反推折前列编号，直到还原成原始 32 列。"""
    columns = set(final_columns)
    for index in range(len(folds) - 1, -1, -1):
        direction = folds[index]
        width_before = N >> index          # 该次折之前的列数
        width_after = width_before // 2    # 该次折之后的列数
        expanded: set[int] = set()
        for c in columns:
            if not (0 <= c < width_after):
                raise ValueError(f"列号 {c} 超出该层宽度 {width_after}")
            if direction == "L":
                expanded.add(width_after + c)
                expanded.add(width_after - 1 - c)
            else:
                expanded.add(c)
                expanded.add(width_before - 1 - c)
        columns = expanded
    expected = 1 << len(folds)
    if len(columns) != expected:
        raise ValueError(f"逆向展开得到 {len(columns)} 列，应为 {expected}")
    return columns


def solve_backward(folds: list[str], punches: list[int]) -> list[int]:
    """把最终列编号逆向展开，直接得到被打穿的原始格子编号。"""
    width = N >> len(folds)
    for p in punches:
        if not (1 <= p <= width):
            raise ValueError(f"打孔列 {p} 超出折后宽度 {width}")
    punched_cols: set[int] = set()
    for p in punches:
        punched_cols.update(_expand_columns(folds, {p - 1}))
    return sorted(c + 1 for c in punched_cols)


# ---------------------------------------------------------------- 生成
def width_after(folds: list[str]) -> int:
    return N >> len(folds)


def build_instances(seed: int = SEED) -> list[dict]:
    rng = random.Random(seed)
    instances: list[dict] = []
    used_patterns: set[tuple[str, ...]] = set()
    used_holes: set[tuple[int, ...]] = set()
    while len(instances) < len(INSTANCE_SPECS):
        fold_count, punch_count = INSTANCE_SPECS[len(instances)]
        folds = tuple(rng.choice(DIRECTIONS) for _ in range(fold_count))
        if folds in used_patterns:
            continue
        width = N >> fold_count
        punches = sorted(rng.sample(range(1, width + 1), punch_count))
        holes = solve_forward(list(folds), punches)
        if len(holes) != (1 << fold_count) * punch_count:
            continue
        if len(holes) >= N:
            continue
        if tuple(holes) in used_holes:
            continue  # 不同折叠+打孔碰巧得到同一答案集，换一组
        used_patterns.add(folds)
        used_holes.add(tuple(holes))
        instances.append(
            {
                "id": len(instances) + 1,
                "panel_count": N,
                "folds": list(folds),
                "punches": punches,
                "width_after": width,
                "holes": holes,
            }
        )
    return instances


def public_view(instances: list[dict]) -> dict:
    return {
        "task": "logic-002",
        "family_version": "fold-and-punch-v1",
        "panel_count": N,
        "instances": [
            {
                "id": inst["id"],
                "folds": inst["folds"],
                "punches": inst["punches"],
            }
            for inst in instances
        ],
    }


def validate(instances: list[dict], seed: int = SEED) -> list[str]:
    problems: list[str] = []
    if len(instances) != len(INSTANCE_SPECS):
        problems.append(f"实例数应为 {len(INSTANCE_SPECS)}，实际 {len(instances)}")
    patterns = [tuple(inst["folds"]) for inst in instances]
    if len(set(patterns)) != len(patterns):
        problems.append(f"折叠序列重复：{patterns}")
    hole_sets = [tuple(inst["holes"]) for inst in instances]
    if len(set(hole_sets)) != len(hole_sets):
        problems.append("批内出现重复答案集")
    for inst in instances:
        folds, punches = inst["folds"], inst["punches"]
        expected_size = (1 << len(folds)) * len(punches)
        if width_after(folds) != N >> len(folds):
            problems.append(f"实例 {inst['id']} 折后宽度计算错误")
        for p in punches:
            if not (1 <= p <= width_after(folds)):
                problems.append(f"实例 {inst['id']} 打孔列 {p} 越界")
        forward = solve_forward(folds, punches)
        backward = solve_backward(folds, punches)
        if forward != backward:
            problems.append(f"实例 {inst['id']} 正逆求解结果不一致：{forward} vs {backward}")
        if backward != inst["holes"]:
            problems.append(f"实例 {inst['id']} 期望答案与求解器不一致")
        if len(backward) != expected_size:
            problems.append(
                f"实例 {inst['id']} 打孔层数 {len(backward)} != 2^{len(folds)}×{len(punches)}"
            )
        if len(set(backward)) != len(backward):
            problems.append(f"实例 {inst['id']} 答案有重复格子")
        if backward != sorted(backward):
            problems.append(f"实例 {inst['id']} 答案未升序")
        if len(backward) >= N:
            problems.append(f"实例 {inst['id']} 答案是全部格子，过于平凡")
    again = build_instances(seed)
    if json.dumps(again, sort_keys=True) != json.dumps(instances, sort_keys=True):
        problems.append("生成器不确定：同一 seed 两次结果不同")
    return problems


def export_visible(path: str, seed: int = SEED) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(public_view(build_instances(seed)), fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="logic-002 生成器")
    parser.add_argument("--export", metavar="PATH", help="导出 Agent 可见实例")
    parser.add_argument("--seed", type=int, default=SEED)
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
        print(f"[OK] 生成器自检通过：{len(INSTANCE_SPECS)} 个实例，双求解器一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
