#!/usr/bin/env python3
"""video-001 ffprobe 硬门槛检查。

用法：
    python3 probe.py --input intro.mp4
    python3 probe.py --selftest        # 用内置 ffprobe JSON 样例验证检查逻辑
退出码：0=通过；1=不达标；2=缺少 ffprobe 或用法错误。
"""

import argparse
import json
import subprocess
import sys


def validate(meta: dict) -> list:
    problems = []
    fmt = meta.get("format") or {}
    streams = [s for s in meta.get("streams") or [] if s.get("codec_type") == "video"]
    if fmt.get("format_name") != "mp4":
        problems.append(f"容器 format_name={fmt.get('format_name')!r}，期望 mp4")
    duration = float(fmt.get("duration") or 0)
    if abs(duration - 30.0) > 0.1:
        problems.append(f"时长 {duration:.3f}s，期望 30.0±0.1")
    if not streams:
        problems.append("没有视频流")
        return problems
    video = streams[0]
    if video.get("codec_name") != "h264":
        problems.append(f"视频编码 {video.get('codec_name')!r}，期望 h264")
    if video.get("pix_fmt") != "yuv420p":
        problems.append(f"像素格式 {video.get('pix_fmt')!r}，期望 yuv420p")
    if (video.get("width"), video.get("height")) != (1920, 1080):
        problems.append(f"分辨率 {video.get('width')}x{video.get('height')}，期望 1920x1080")
    if video.get("avg_frame_rate") != "30/1":
        problems.append(f"avg_frame_rate={video.get('avg_frame_rate')!r}，期望 30/1")
    if video.get("r_frame_rate") != "30/1":
        problems.append(f"r_frame_rate={video.get('r_frame_rate')!r}，期望 30/1")
    frames = video.get("nb_frames")
    if frames is not None and int(frames) != 900:
        problems.append(f"帧数 {frames}，期望 900")
    audio = [s for s in meta.get("streams") or [] if s.get("codec_type") == "audio"]
    if audio:
        problems.append("存在音频流；契约要求单视频流无音频")
    return problems


def run_ffprobe(path: str) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {proc.stderr[-500:]}")
    return json.loads(proc.stdout)


GOOD = {
    "format": {"format_name": "mp4", "duration": "30.000000"},
    "streams": [
        {
            "codec_type": "video", "codec_name": "h264", "pix_fmt": "yuv420p",
            "width": 1920, "height": 1080, "avg_frame_rate": "30/1",
            "r_frame_rate": "30/1", "nb_frames": "900",
        }
    ],
}
BAD = json.loads(json.dumps(GOOD))
BAD["format"]["duration"] = "24.000000"
BAD["streams"][0].update({"width": 1280, "height": 720, "avg_frame_rate": "25/1",
                          "r_frame_rate": "25/1", "codec_name": "mpeg4", "nb_frames": "600"})


def selftest() -> int:
    failures = []
    if validate(GOOD):
        failures.append("GOOD 样例应当通过: " + "; ".join(validate(GOOD)))
    if not validate(BAD):
        failures.append("BAD 样例应当失败")
    if failures:
        for item in failures:
            print("SELFTEST FAIL:", item)
        return 1
    print("probe.py selftest: 2/2 样例判定正确")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.input:
        parser.error("--input 或 --selftest 必填")
    try:
        meta = run_ffprobe(args.input)
    except FileNotFoundError:
        print("INFRA: 缺少 ffprobe，无法执行完整导出验收（环境错误，按 docs/02 §8 重试/换机）", file=sys.stderr)
        return 2
    problems = validate(meta)
    if problems:
        for item in problems:
            print("FAIL:", item)
        return 1
    print("PASS: ffprobe 硬门槛通过（mp4/h264/yuv420p/1920x1080/30fps/30.0s/900帧/无音频）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
