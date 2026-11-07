#!/usr/bin/env python3
"""video-001 抽帧结构检查（标准库，不依赖 ffmpeg）。

用法：
    # 分析一个目录下按时间命名的 PPM 帧（t_<time>.ppm）
    python3 frames.py --dir /tmp/frames
    # 自检：用合成帧验证检查逻辑本身
    python3 frames.py --selftest

契约（README.md）：1920x1080、深色背景、主色强调元素、五段结构、结尾有结束卡。
退出码：0=通过；1=检查失败；2=用法/IO 错误。
"""

import argparse
import os
import sys

WIDTH = 1920
HEIGHT = 1080
ACCENT = (64, 120, 255)
BG = (10, 12, 20)
TIMES = (1.0, 5.0, 12.0, 20.0, 26.0, 29.0)


def parse_ppm(data: bytes):
    if not data.startswith(b"P6"):
        raise ValueError("不是 P6 PPM")
    tokens = []
    index = 2
    while len(tokens) < 3:
        while index < len(data) and data[index:index + 1].isspace():
            index += 1
        if data[index:index + 1] == b"#":
            while data[index:index + 1] != b"\n":
                index += 1
            continue
        start = index
        while index < len(data) and not data[index:index + 1].isspace():
            index += 1
        tokens.append(int(data[start:index]))
    index += 1
    width, height, maxval = tokens
    if maxval != 255:
        raise ValueError("只支持 maxval=255")
    pixels = data[index:index + width * height * 3]
    if len(pixels) != width * height * 3:
        raise ValueError("像素数据长度不符")
    return width, height, pixels


def _pixel(pixels, width, x, y):
    offset = (y * width + x) * 3
    return pixels[offset], pixels[offset + 1], pixels[offset + 2]


def analyze(data: bytes) -> dict:
    width, height, pixels = parse_ppm(data)
    total = width * height
    step = 4  # 每 4 像素采样一次，够用且快
    luma_sum = 0
    accent = 0
    ink = 0
    samples = 0
    for y in range(0, height, step):
        for x in range(0, width, step):
            r, g, b = _pixel(pixels, width, x, y)
            luma = (r * 299 + g * 587 + b * 114) // 1000
            luma_sum += luma
            if luma > 40:
                ink += 1
            if max(r, g, b) - min(r, g, b) > 50 and max(r, g, b) > 75:
                accent += 1
            samples += 1
    return {
        "width": width,
        "height": height,
        "mean_luma": luma_sum / samples,
        "ink_ratio": ink / samples,
        "accent_ratio": accent / samples,
    }


def mean_abs_diff(a: bytes, b: bytes) -> float:
    wa, ha, pa = parse_ppm(a)
    wb, hb, pb = parse_ppm(b)
    if (wa, ha) != (wb, hb):
        return 999.0
    step = 8
    total = 0.0
    count = 0
    for y in range(0, ha, step):
        for x in range(0, wa, step):
            ra, ga, ba = _pixel(pa, wa, x, y)
            rb, gb, bb = _pixel(pb, wb, x, y)
            la = (ra * 299 + ga * 587 + ba * 114) // 1000
            lb = (rb * 299 + gb * 587 + bb * 114) // 1000
            total += abs(la - lb)
            count += 1
    return total / count


def load_frame(directory: str, moment: float) -> bytes:
    path = os.path.join(directory, f"t_{moment:.1f}.ppm")
    with open(path, "rb") as handle:
        return handle.read()


def check_directory(directory: str) -> list:
    problems = []
    try:
        frames = {moment: load_frame(directory, moment) for moment in TIMES}
    except OSError as exc:
        return [f"缺少抽帧文件: {exc}"]
    stats = {moment: analyze(data) for moment, data in frames.items()}
    for moment, stat in stats.items():
        if (stat["width"], stat["height"]) != (WIDTH, HEIGHT):
            problems.append(
                f"t={moment}: 分辨率 {stat['width']}x{stat['height']}，期望 {WIDTH}x{HEIGHT}"
            )
            return problems
    if stats[1.0]["mean_luma"] > 90:
        problems.append(f"t=1.0: 背景不够深（mean_luma={stats[1.0]['mean_luma']:.1f}）")
    if stats[1.0]["accent_ratio"] < 0.001:
        problems.append("t=1.0: logo 揭示段没有主色元素")
    if mean_abs_diff(frames[5.0], frames[12.0]) < 3.0:
        problems.append("t=5.0 与 t=12.0 画面几乎相同，动画没有推进")
    accent_frames = [moment for moment, stat in stats.items() if stat["accent_ratio"] >= 0.001]
    if len(accent_frames) < 4:
        problems.append(f"主色元素只出现在 {len(accent_frames)}/6 个采样帧，契约要求品牌主色贯穿全片")
    strongest = max(stat["accent_ratio"] for stat in stats.values())
    if strongest < 0.003:
        problems.append(f"全片缺少品牌主色强调（最大主色占比 {strongest:.4f}）")
    if stats[29.0]["ink_ratio"] < 0.005:
        problems.append("t=29.0: 结束卡内容过少（ink_ratio 过低）")
    return problems


# ---------------------------------------------------------------- selftest
def _make_ppm(path: str, rects, background=BG, width=WIDTH, height=HEIGHT) -> None:
    canvas = bytearray(bytes(background) * (width * height))
    for x, y, w, h, color in rects:
        row = bytes(color) * w
        for yy in range(y, min(height, y + h)):
            start = (yy * width + x) * 3
            canvas[start:start + w * 3] = row
    with open(path, "wb") as handle:
        handle.write(b"P6\n%d %d\n255\n" % (width, height))
        handle.write(bytes(canvas))


def selftest() -> int:
    import tempfile

    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        good = os.path.join(tmp, "good")
        os.makedirs(good)
        _make_ppm(os.path.join(good, "t_1.0.ppm"), [(860, 380, 200, 200, ACCENT)])
        _make_ppm(os.path.join(good, "t_5.0.ppm"), [(180, 380, 900, 80, (235, 238, 245)), (180, 480, 200, 20, ACCENT)])
        _make_ppm(os.path.join(good, "t_12.0.ppm"), [(300, 500, 1200, 200, (235, 238, 245)), (300, 640, 200, 20, ACCENT)])
        _make_ppm(os.path.join(good, "t_20.0.ppm"), [(0, 0, WIDTH, HEIGHT, ACCENT)])
        _make_ppm(os.path.join(good, "t_26.0.ppm"), [(700, 300, 500, 400, ACCENT), (0, 0, 40, 16, ACCENT)])
        _make_ppm(os.path.join(good, "t_29.0.ppm"), [(700, 400, 520, 200, (235, 238, 245)), (760, 660, 200, 20, ACCENT)])
        if check_directory(good):
            failures.append("good 合成帧集应当通过: " + "; ".join(check_directory(good)))

        blank = os.path.join(tmp, "blank")
        os.makedirs(blank)
        for moment in TIMES:
            _make_ppm(os.path.join(blank, f"t_{moment:.1f}.ppm"), [])
        problems = check_directory(blank)
        if not problems:
            failures.append("全黑帧集应当失败")

        small = os.path.join(tmp, "small")
        os.makedirs(small)
        for moment in TIMES:
            _make_ppm(os.path.join(small, f"t_{moment:.1f}.ppm"), [], width=1280, height=720)
        if not check_directory(small):
            failures.append("720p 帧集应当失败")

        static = os.path.join(tmp, "static")
        os.makedirs(static)
        _make_ppm(os.path.join(static, "t_1.0.ppm"), [(860, 380, 200, 200, ACCENT)])
        _make_ppm(os.path.join(static, "t_5.0.ppm"), [(180, 380, 900, 80, (235, 238, 245))])
        _make_ppm(os.path.join(static, "t_12.0.ppm"), [(180, 380, 900, 80, (235, 238, 245))])
        _make_ppm(os.path.join(static, "t_20.0.ppm"), [(0, 0, WIDTH, HEIGHT, ACCENT)])
        _make_ppm(os.path.join(static, "t_26.0.ppm"), [(700, 300, 500, 400, ACCENT)])
        _make_ppm(os.path.join(static, "t_29.0.ppm"), [(700, 400, 520, 200, (235, 238, 245))])
        if not check_directory(static):
            failures.append("静止帧集应当失败（t=5 与 t=12 相同）")

    if failures:
        for item in failures:
            print("SELFTEST FAIL:", item)
        return 1
    print("frames.py selftest: 4/4 合成用例判定正确")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir")
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    if not args.dir:
        parser.error("--dir 或 --selftest 必填")
    problems = check_directory(args.dir)
    if problems:
        for item in problems:
            print("FAIL:", item)
        return 1
    print("PASS: 6 帧结构检查通过（分辨率/深色背景/主色/动画推进/结束卡）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
