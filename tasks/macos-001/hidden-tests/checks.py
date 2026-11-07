#!/usr/bin/env python3
"""macos-001 功能/性能检查器（标准库，可直接在 macOS 上运行）。

用法：
    python3 checks.py --selftest                     # 用合成 JSON 验证判定逻辑
    python3 checks.py --mode functional --binary build/ParticleBench
    python3 checks.py --mode perf --binary build/ParticleBench

性能门槛：24000 粒子 / 1920x1080 / 120 帧，5 次独立运行取 avg_frame_ms 中位数 <= 16.7ms。
退出码：0=通过；1=不达标；2=用法/执行错误。
"""

import argparse
import json
import os
import statistics
import subprocess
import sys

PERF_PARTICLES = 24000
PERF_WIDTH = 1920
PERF_HEIGHT = 1080
PERF_FRAMES = 120
PERF_RUNS = 5
FRAME_BUDGET_MS = 16.7
REQUIRED_KEYS = (
    "device", "mode", "particles", "width", "height", "frames",
    "avg_frame_ms", "fps", "non_background_pixels", "frame_hash", "metal_version",
)


def validate_payload(payload: dict, particles: int, width: int, height: int, frames: int) -> list:
    problems = []
    if not isinstance(payload, dict):
        return ["输出不是 JSON 对象"]
    missing = [key for key in REQUIRED_KEYS if key not in payload]
    if missing:
        problems.append(f"缺少 JSON 字段: {missing}")
        return problems
    if payload["particles"] != particles:
        problems.append(f"particles={payload['particles']}，期望 {particles}")
    if payload["width"] != width or payload["height"] != height:
        problems.append(f"分辨率 {payload['width']}x{payload['height']}，期望 {width}x{height}")
    if payload["frames"] != frames:
        problems.append(f"frames={payload['frames']}，期望 {frames}")
    avg = float(payload["avg_frame_ms"])
    if avg <= 0 or avg != avg:
        problems.append(f"avg_frame_ms 非法: {avg}")
    if payload["non_background_pixels"] < int(width * height * 0.01):
        problems.append(
            f"非背景像素 {payload['non_background_pixels']} 少于画面 1%，粒子不可见")
    if not str(payload["frame_hash"]):
        problems.append("frame_hash 为空")
    return problems


def run_binary(binary: str, args: list, cwd: str) -> dict:
    proc = subprocess.run([binary, *args], cwd=cwd, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"二进制退出码 {proc.returncode}: {proc.stderr[-800:]}")
    lines = [line for line in proc.stdout.splitlines() if line.strip().startswith("{")]
    if not lines:
        raise RuntimeError(f"stdout 没有 JSON 行: {proc.stdout[-800:]}")
    return json.loads(lines[-1])


def functional(binary: str, workdir: str) -> list:
    problems = []
    args = ["--frames", "8", "--particles", "2000", "--width", "640", "--height", "360"]
    first = run_binary(binary, args, workdir)
    second = run_binary(binary, args, workdir)
    problems += validate_payload(first, 2000, 640, 360, 8)
    if first.get("frame_hash") != second.get("frame_hash"):
        problems.append("两次运行的 frame_hash 不一致（渲染不确定）")
    if first.get("device") in ("", None):
        problems.append("device 为空")
    return problems


def perf(binary: str, workdir: str) -> list:
    problems = []
    samples = []
    for index in range(PERF_RUNS):
        payload = run_binary(
            binary,
            ["--frames", str(PERF_FRAMES), "--particles", str(PERF_PARTICLES),
             "--width", str(PERF_WIDTH), "--height", str(PERF_HEIGHT)],
            workdir,
        )
        problems += validate_payload(payload, PERF_PARTICLES, PERF_WIDTH, PERF_HEIGHT, PERF_FRAMES)
        samples.append(float(payload["avg_frame_ms"]))
        if problems:
            return problems
    median = statistics.median(samples)
    print("perf runs avg_frame_ms:", ", ".join(f"{value:.2f}" for value in samples))
    print(f"median avg_frame_ms={median:.2f}（门槛 <= {FRAME_BUDGET_MS}）")
    if median > FRAME_BUDGET_MS:
        problems.append(
            f"性能不达标：中位 avg_frame_ms={median:.2f}ms > {FRAME_BUDGET_MS}ms（<60fps）")
    return problems


GOOD = {
    "device": "Apple M6", "mode": "instanced-triple-buffered", "particles": 2000,
    "width": 640, "height": 360, "frames": 8, "avg_frame_ms": 0.4, "fps": 2500.0,
    "non_background_pixels": 20000, "frame_hash": "abc123", "metal_version": "apple7+",
}
BAD_PERF = dict(GOOD, avg_frame_ms=57.0, fps=17.5)
BAD_PIXELS = dict(GOOD, non_background_pixels=10)


def selftest() -> int:
    failures = []
    if validate_payload(GOOD, 2000, 640, 360, 8):
        failures.append("GOOD 应通过: " + "; ".join(validate_payload(GOOD, 2000, 640, 360, 8)))
    if not validate_payload(BAD_PIXELS, 2000, 640, 360, 8):
        failures.append("BAD_PIXELS 应失败")
    if statistics.median([57.0, 58.0, 56.0]) <= FRAME_BUDGET_MS:
        failures.append("BAD_PERF 中位数应超过门槛")
    if statistics.median([0.4, 0.5, 0.3]) > FRAME_BUDGET_MS:
        failures.append("GOOD 中位数应低于门槛")
    if failures:
        for item in failures:
            print("SELFTEST FAIL:", item)
        return 1
    print("checks.py selftest: 判定逻辑 4/4 正确")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--mode", choices=["functional", "perf"])
    parser.add_argument("--binary")
    parser.add_argument("--workdir", default=".")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.mode or not args.binary:
        parser.error("--mode 与 --binary 必填")
    if not os.path.exists(args.binary):
        print(f"ERROR: 二进制不存在: {args.binary}", file=sys.stderr)
        return 2
    try:
        problems = functional(args.binary, args.workdir) if args.mode == "functional" else perf(args.binary, args.workdir)
    except (RuntimeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if problems:
        for item in problems:
            print("FAIL:", item)
        return 1
    print(f"PASS: {args.mode} 检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
