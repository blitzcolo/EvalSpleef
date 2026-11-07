#!/usr/bin/env python3
"""检查候选的 --plan 节拍表：5 段以上、总时长 30 秒、时间轴连续。

用法：python3 plan_check.py <候选工程目录>
退出码：0=通过；1=不达标；2=用法/执行错误。
"""

import subprocess
import sys


def parse_plan(output: str):
    scenes = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or parts[0] == "scene":
            continue
        try:
            index, start, end, name = int(parts[0]), float(parts[1]), float(parts[2]), parts[3]
        except ValueError:
            continue
        scenes.append((index, start, end, name))
    return scenes


def main(argv) -> int:
    if len(argv) != 2:
        print("用法: plan_check.py <候选工程目录>", file=sys.stderr)
        return 2
    candidate = argv[1]
    proc = subprocess.run(
        [sys.executable, "intro.py", "--plan"], cwd=candidate, capture_output=True, text=True, timeout=60
    )
    if proc.returncode != 0:
        print("FAIL: --plan 入口执行失败:", proc.stderr[-500:])
        return 1
    scenes = parse_plan(proc.stdout)
    problems = []
    if len(scenes) < 5:
        problems.append(f"场景数 {len(scenes)}，契约要求五段（>=5）")
    if scenes:
        scenes.sort()
        if abs(scenes[0][1] - 0.0) > 0.01:
            problems.append(f"首段起点 {scenes[0][1]}，应为 0")
        if abs(scenes[-1][2] - 30.0) > 0.01:
            problems.append(f"末段终点 {scenes[-1][2]}，应为 30")
        for previous, current in zip(scenes, scenes[1:]):
            if abs(previous[2] - current[1]) > 0.01:
                problems.append(f"节拍不连续：{previous[3]} 结束于 {previous[2]}，下一段从 {current[1]} 开始")
    if problems:
        for item in problems:
            print("FAIL:", item)
        return 1
    print(f"PASS: 节拍表 {len(scenes)} 段，时间轴 0→30s 连续")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
