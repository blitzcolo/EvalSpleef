#!/usr/bin/env python3
"""intro-render：30 秒片头生成器（半成品）。

契约见 README.md。标准库实现；完整导出需要系统 ffmpeg（通过 stdin 管道收 rgb24 帧）。
"""

import argparse
import subprocess
import sys
import time

# ---- 画面参数（半成品：与契约不符，需按 README 修正）----
WIDTH = 1280
HEIGHT = 720
FPS = 25
SECONDS = 24
ENCODE = [
    "-c:v", "mpeg4",
    "-pix_fmt", "yuv420p",
    "-movflags", "+faststart",
]

# 调色板：深色底 + 一个主色
BG = (10, 12, 20)
FG = (235, 238, 245)
ACCENT = (64, 120, 255)
MUTED = (120, 128, 150)

FONT = {
    " ": ["00000"] * 7,
    "A": ["01110", "10001", "10001", "11111", "10001", "10001", "10001"],
    "B": ["11110", "10001", "10001", "11110", "10001", "10001", "11110"],
    "C": ["01111", "10000", "10000", "10000", "10000", "10000", "01111"],
    "D": ["11110", "10001", "10001", "10001", "10001", "10001", "11110"],
    "E": ["11111", "10000", "10000", "11110", "10000", "10000", "11111"],
    "F": ["11111", "10000", "10000", "11110", "10000", "10000", "10000"],
    "G": ["01111", "10000", "10000", "10111", "10001", "10001", "01110"],
    "H": ["10001", "10001", "10001", "11111", "10001", "10001", "10001"],
    "I": ["11111", "00100", "00100", "00100", "00100", "00100", "11111"],
    "L": ["10000", "10000", "10000", "10000", "10000", "10000", "11111"],
    "M": ["10001", "11011", "10101", "10101", "10001", "10001", "10001"],
    "N": ["10001", "11001", "10101", "10011", "10001", "10001", "10001"],
    "O": ["01110", "10001", "10001", "10001", "10001", "10001", "01110"],
    "P": ["11110", "10001", "10001", "11110", "10000", "10000", "10000"],
    "R": ["11110", "10001", "10001", "11110", "10100", "10010", "10001"],
    "S": ["01111", "10000", "10000", "01110", "00001", "00001", "11110"],
    "T": ["11111", "00100", "00100", "00100", "00100", "00100", "00100"],
    "U": ["10001", "10001", "10001", "10001", "10001", "10001", "01110"],
    "V": ["10001", "10001", "10001", "10001", "10001", "01010", "00100"],
    "W": ["10001", "10001", "10001", "10101", "10101", "11011", "10001"],
    "X": ["10001", "10001", "01010", "00100", "01010", "10001", "10001"],
    "Y": ["10001", "10001", "01010", "00100", "00100", "00100", "00100"],
    "Z": ["11111", "00001", "00010", "00100", "01000", "10000", "11111"],
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
}


class Canvas:
    """rgb24 帧缓冲；bytes 切片赋值，避免逐像素 Python 循环。"""

    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.pixels = bytearray(width * height * 3)

    def fill(self, color):
        row = bytes(color) * self.width
        for y in range(self.height):
            start = y * self.width * 3
            self.pixels[start:start + self.width * 3] = row

    def rect(self, x, y, w, h, color):
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.width, int(x + w)), min(self.height, int(y + h))
        if x1 <= x0 or y1 <= y0:
            return
        row = bytes(color) * (x1 - x0)
        for yy in range(y0, y1):
            start = (yy * self.width + x0) * 3
            self.pixels[start:start + (x1 - x0) * 3] = row

    def text(self, x, y, size, text, color, spacing=1):
        """内置 5x7 点阵，按 size 放大；越界裁剪。"""
        cursor = int(x)
        for ch in text.upper():
            glyph = FONT.get(ch)
            if glyph is None:
                cursor += 6 * size
                continue
            for gy, row in enumerate(glyph):
                for gx, bit in enumerate(row):
                    if bit == "1":
                        self.rect(cursor + gx * size, int(y) + gy * size, size, size, color)
            cursor += (5 + spacing) * size
        return cursor

    def to_bytes(self):
        return bytes(self.pixels)


def ease_out(t):
    return 1.0 - (1.0 - t) ** 3


def render_frame(t, canvas):
    """渲染 t 秒处的画面（半成品：只有一段静态标题）。"""
    canvas.fill(BG)
    width, height = canvas.width, canvas.height
    canvas.text(width // 2 - 5 * 26, height // 2 - 50, 8, "INTRO", FG)
    canvas.rect(0, height - 8, width, 8, ACCENT)
    return canvas


def plan():
    print("scene\tstart\tend\tname")
    print("1\t0.0\t24.0\ttitle-placeholder")


def render_video(path):
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{WIDTH}x{HEIGHT}", "-r", str(FPS), "-i", "-",
           "-t", str(SECONDS), *ENCODE, path]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    canvas = Canvas(WIDTH, HEIGHT)
    total = FPS * SECONDS
    started = time.time()
    for index in range(total):
        render_frame(index / FPS, canvas)
        proc.stdin.write(canvas.to_bytes())
    proc.stdin.close()
    code = proc.wait()
    print(f"WROTE {path} frames={total} elapsed={time.time() - started:.1f}s code={code}")
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(prog="intro")
    parser.add_argument("--out")
    parser.add_argument("--render-frame", type=float)
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args(argv)
    if args.plan:
        plan()
        return 0
    if args.render_frame is not None:
        canvas = Canvas(WIDTH, HEIGHT)
        render_frame(args.render_frame, canvas)
        out = args.out or "frame.ppm"
        with open(out, "wb") as handle:
            handle.write(b"P6\n%d %d\n255\n" % (WIDTH, HEIGHT))
            handle.write(canvas.to_bytes())
        print(f"WROTE {out} {WIDTH}x{HEIGHT}")
        return 0
    if not args.out:
        parser.error("--out 或 --render-frame 必填")
    return render_video(args.out)


if __name__ == "__main__":
    sys.exit(main())
