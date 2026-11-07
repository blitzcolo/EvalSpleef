#!/usr/bin/env python3
"""image_metrics 自测：合成 PPM/PNG，验证解码与差异指标。"""
from __future__ import annotations

import struct
import sys
import tempfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import image_metrics as im  # noqa: E402

passed = 0
failed = 0


def check(cond: bool, label: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [OK]   {label}")
    else:
        failed += 1
        print(f"  [FAIL] {label}")


def write_ppm(path: Path, width: int, height: int, rgb: bytes) -> None:
    path.write_bytes(b"P6\n%d %d\n255\n" % (width, height) + rgb)


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: none
        start = y * width * 3
        raw.extend(rgb[start : start + width * 3])

    def chunk(ctype: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + ctype + payload + struct.pack(">I", zlib.crc32(ctype + payload) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")
    )


def main() -> int:
    width, height = 4, 3
    base = bytes(((x * 17 + y * 29 + c * 41) % 256) for y in range(height) for x in range(width) for c in range(3))
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        ref_ppm = tmpdir / "ref.ppm"
        cand_ppm = tmpdir / "cand.ppm"
        ref_png = tmpdir / "ref.png"
        cand_png = tmpdir / "cand.png"
        write_ppm(ref_ppm, width, height, base)
        write_ppm(cand_ppm, width, height, base)
        write_png(ref_png, width, height, base)
        write_png(cand_png, width, height, base)

        a = im.read_image(ref_ppm)
        b = im.read_image(ref_png)
        check(a.rgb == base and b.rgb == base, "PPM 与 PNG 解码结果一致")
        m = im.compare(a, b)
        check(m["mean_abs_diff"] == 0.0 and m["match_ratio"] == 1.0, "相同图像：MAD=0、匹配率=1")
        check(m["psnr"] >= 99.0, "相同图像：PSNR 记 99")

        dark = bytes(max(0, v - 40) for v in base)
        write_ppm(cand_ppm, width, height, dark)
        m2 = im.compare(im.read_image(ref_ppm), im.read_image(cand_ppm))
        check(m2["mean_abs_diff"] > 12.0, "变暗 40 级：MAD 超阈值被识别")
        check(m2["match_ratio"] < 0.92, "变暗 40 级：匹配率低于阈值")

        try:
            im.compare(im.read_image(ref_ppm), im.Image(width + 1, height, base))
        except im.ImageDecodeError:
            check(True, "尺寸不一致：抛出解码错误")
        else:
            check(False, "尺寸不一致：应当抛错")

        bad = tmpdir / "bad.ppm"
        bad.write_bytes(b"P5\n2 2\n255\n" + b"\x00" * 4)
        try:
            im.read_image(bad)
        except im.ImageDecodeError:
            check(True, "非 P6 的 PPM：抛错")
        else:
            check(False, "非 P6 的 PPM：应当抛错")

    print(f"结果：{passed} 通过 / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
