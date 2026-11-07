#!/usr/bin/env python3
"""vulkan-001 截图相似度工具：纯标准库读 PPM(P6) / PNG(RGB/RGBA, 8bit)，算差异指标。

用法：
    python3 image_metrics.py <reference.ppm|png> <candidate.ppm|png> [--mad 12] [--ratio 0.92]

退出码：0=相似度达标；1=不达标；2=用法/解码错误。
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path


class ImageDecodeError(ValueError):
    pass


class Image:
    def __init__(self, width: int, height: int, rgb: bytes) -> None:
        self.width = width
        self.height = height
        self.rgb = rgb  # 每像素 3 字节

    @property
    def pixels(self) -> int:
        return self.width * self.height


def read_ppm(path: Path) -> Image:
    data = path.read_bytes()
    if not data.startswith(b"P6"):
        raise ImageDecodeError(f"{path.name}: 不是 P6 PPM")
    fields: list[int] = []
    pos = 2
    while len(fields) < 3:
        while pos < len(data) and data[pos : pos + 1].isspace():
            pos += 1
        if pos < len(data) and data[pos : pos + 1] == b"#":
            while pos < len(data) and data[pos : pos + 1] != b"\n":
                pos += 1
            continue
        start = pos
        while pos < len(data) and not data[pos : pos + 1].isspace():
            pos += 1
        fields.append(int(data[start:pos]))
    pos += 1  # 跳过 maxval 后的单个空白
    width, height, maxval = fields
    if maxval != 255:
        raise ImageDecodeError(f"{path.name}: 只支持 maxval=255")
    need = width * height * 3
    rgb = data[pos : pos + need]
    if len(rgb) != need:
        raise ImageDecodeError(f"{path.name}: 像素数据不足（期望 {need} 字节）")
    return Image(width, height, rgb)


def read_png(path: Path) -> Image:
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ImageDecodeError(f"{path.name}: 不是 PNG")
    pos = 8
    width = height = bit_depth = color_type = 0
    idat = bytearray()
    while pos + 8 <= len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        payload = data[pos + 8 : pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[:10])
        elif ctype == b"IDAT":
            idat.extend(payload)
        elif ctype == b"IEND":
            break
    if width <= 0 or height <= 0:
        raise ImageDecodeError(f"{path.name}: 缺少 IHDR")
    if bit_depth != 8 or color_type not in (2, 6):
        raise ImageDecodeError(f"{path.name}: 只支持 8bit RGB/RGBA PNG")
    channels = 3 if color_type == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * channels
    out = bytearray(width * height * 3)
    prev = bytearray(stride)
    offset = 0
    for y in range(height):
        if offset >= len(raw):
            raise ImageDecodeError(f"{path.name}: 像素数据不足")
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset : offset + stride])
        offset += stride
        if len(line) != stride:
            raise ImageDecodeError(f"{path.name}: 行数据不足")
        for x in range(stride):
            a = line[x - channels] if x >= channels else 0
            b = prev[x]
            c = prev[x - channels] if x >= channels else 0
            if filter_type == 0:
                val = line[x]
            elif filter_type == 1:
                val = (line[x] + a) & 0xFF
            elif filter_type == 2:
                val = (line[x] + b) & 0xFF
            elif filter_type == 3:
                val = (line[x] + ((a + b) >> 1)) & 0xFF
            elif filter_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                val = (line[x] + pred) & 0xFF
            else:
                raise ImageDecodeError(f"{path.name}: 未知 filter {filter_type}")
            line[x] = val
        for x in range(width):
            src = x * channels
            dst = (y * width + x) * 3
            out[dst : dst + 3] = line[src : src + 3]
        prev = line
    return Image(width, height, bytes(out))


def read_image(path: str | Path) -> Image:
    p = Path(path)
    if not p.is_file():
        raise ImageDecodeError(f"文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix == ".ppm":
        return read_ppm(p)
    if suffix == ".png":
        return read_png(p)
    raise ImageDecodeError(f"不支持的扩展名：{suffix}")


def compare(reference: Image, candidate: Image, tolerance: int = 8) -> dict:
    if (reference.width, reference.height) != (candidate.width, candidate.height):
        raise ImageDecodeError(
            f"尺寸不一致：参考 {reference.width}x{reference.height}，候选 {candidate.width}x{candidate.height}"
        )
    total = len(reference.rgb)
    diff_sum = 0
    sq_sum = 0
    within = 0
    for i in range(total):
        d = abs(reference.rgb[i] - candidate.rgb[i])
        diff_sum += d
        sq_sum += d * d
        if d <= tolerance:
            within += 1
    mad = diff_sum / total
    mse = sq_sum / total
    psnr = 99.0 if mse == 0 else 10.0 * (__import__("math").log10(255.0 * 255.0 / mse))
    return {
        "width": reference.width,
        "height": reference.height,
        "mean_abs_diff": round(mad, 3),
        "psnr": round(psnr, 2),
        "match_ratio": round(within / total, 4),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference")
    ap.add_argument("candidate")
    ap.add_argument("--mad", type=float, default=12.0, help="平均绝对差上限")
    ap.add_argument("--ratio", type=float, default=0.92, help="逐通道接近比例下限")
    ap.add_argument("--tolerance", type=int, default=8, help="判定为接近的单通道差阈值")
    args = ap.parse_args(argv)
    try:
        metrics = compare(read_image(args.reference), read_image(args.candidate), args.tolerance)
    except ImageDecodeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    passed = metrics["mean_abs_diff"] <= args.mad and metrics["match_ratio"] >= args.ratio
    metrics["passed"] = passed
    print(json.dumps(metrics, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
