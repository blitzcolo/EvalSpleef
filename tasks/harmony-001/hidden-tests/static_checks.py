#!/usr/bin/env python3
"""harmony-001 静态检查：工程结构、API 契约、权限与构建接线（只用标准库）。

退出码：0=通过；1=检查失败；2=用法/环境错误；3=契约文件被修改（硬门槛）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

EXPORT_NAMES = ["grayscale", "adjustBrightness", "adjustContrast", "boxBlur3", "rotate90"]
LOCKS_FILE = pathlib.Path(__file__).with_name("locks.json")


class Checker:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.failures: list[str] = []
        self.lock_tampered = False

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def read(self, rel: str) -> str | None:
        p = self.root / rel
        if not p.is_file():
            self.fail(f"缺少文件：{rel}")
            return None
        return p.read_text(encoding="utf-8", errors="replace")

    def check_required_files(self) -> None:
        required = [
            "build-profile.json5",
            "entry/src/main/module.json5",
            "entry/src/main/ets/pages/Index.ets",
            "entry/src/main/cpp/CMakeLists.txt",
            "entry/src/main/cpp/napi_init.cpp",
            "entry/src/main/cpp/image_ops.cpp",
            "entry/src/main/cpp/image_ops.h",
            "entry/src/main/cpp/types/libimageops/index.d.ts",
        ]
        for rel in required:
            if not (self.root / rel).is_file():
                self.fail(f"缺少工程文件：{rel}")

    def check_locks(self) -> None:
        if not LOCKS_FILE.is_file():
            self.fail("隐藏锁文件缺失（评测环境损坏）")
            return
        locks = json.loads(LOCKS_FILE.read_text(encoding="utf-8"))
        for rel, want in locks.items():
            p = self.root / rel
            if not p.is_file():
                self.fail(f"契约文件缺失：{rel}")
                return
            got = hashlib.sha256(p.read_bytes()).hexdigest()
            if got != want:
                self.lock_tampered = True
                self.fail(f"契约文件被修改：{rel}（原样保留，不得改动）")

    def check_dts(self) -> None:
        text = self.read("entry/src/main/cpp/types/libimageops/index.d.ts") or ""
        for name in EXPORT_NAMES:
            if not re.search(rf"export\s+const\s+{name}\s*:", text):
                self.fail(f"类型声明缺少导出：{name}")
        if "ArrayBuffer" not in text:
            self.fail("类型声明没有使用 ArrayBuffer 传像素数据")

    def check_napi(self) -> None:
        text = self.read("entry/src/main/cpp/napi_init.cpp") or ""
        if "image_ops.h" not in text:
            self.fail("napi_init.cpp 没有 include image_ops.h")
        for name in EXPORT_NAMES:
            if not re.search(rf'"{name}"', text):
                self.fail(f"NAPI 没有注册函数：{name}")
        if "napi_module_register" not in text:
            self.fail("NAPI 模块没有注册（napi_module_register）")
        if "napi_throw_type_error" not in text:
            self.fail("NAPI 层没有对非法参数抛错")

    def check_cmake(self) -> None:
        text = self.read("entry/src/main/cpp/CMakeLists.txt") or ""
        if "image_ops.cpp" not in text:
            self.fail("CMakeLists.txt 没有编译 image_ops.cpp")
        if "libace_napi.z.so" not in text:
            self.fail("CMakeLists.txt 没有链接 libace_napi.z.so")

    def check_module(self) -> None:
        text = self.read("entry/src/main/module.json5") or ""
        if "mainElement" not in text:
            self.fail("module.json5 缺少 mainElement")
        if "pages" not in text:
            self.fail("module.json5 缺少 pages 配置")
        forbidden = ["ohos.permission.READ_IMAGEVIDEO", "ohos.permission.READ_MEDIA"]
        for perm in forbidden:
            if perm in text:
                self.fail(f"多余的敏感权限声明：{perm}（系统相册选择器不需要该权限）")

    def check_ui(self) -> None:
        text = self.read("entry/src/main/ets/pages/Index.ets") or ""
        if "libimageops.so" not in text:
            self.fail("Index.ets 没有导入 NAPI 模块 libimageops.so")
        for name in EXPORT_NAMES:
            if name not in text:
                self.fail(f"Index.ets 没有调用 {name}")
        if "readPixelsToBuffer" not in text:
            self.fail("Index.ets 没有从 PixelMap 读出像素缓冲区")
        if "createPixelMap" not in text:
            self.fail("Index.ets 没有把处理后的缓冲区还原成 PixelMap")

    def run(self) -> int:
        self.check_required_files()
        self.check_locks()
        self.check_dts()
        self.check_napi()
        self.check_cmake()
        self.check_module()
        self.check_ui()
        if self.lock_tampered:
            print("FAIL: 契约文件被修改（硬门槛 HG3）", file=sys.stderr)
            return 3
        if self.failures:
            for f in self.failures:
                print(f"FAIL: {f}", file=sys.stderr)
            return 1
        print("PASS: harmony-001 静态检查通过")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", nargs="?", default=str(pathlib.Path(__file__).parent.parent / "starter"))
    ap.add_argument("--locks-only", action="store_true", help="只校验契约文件哈希（硬门槛 HG3）")
    args = ap.parse_args()
    root = pathlib.Path(args.candidate).resolve()
    if not root.is_dir():
        print(f"ERROR: 候选目录不存在：{root}", file=sys.stderr)
        return 2
    checker = Checker(root)
    if args.locks_only:
        checker.check_locks()
        if checker.lock_tampered:
            print("FAIL: 契约文件被修改（硬门槛 HG3）", file=sys.stderr)
            return 3
        if checker.failures:
            for f in checker.failures:
                print(f"FAIL: {f}", file=sys.stderr)
            return 1
        print("PASS: 契约文件哈希一致")
        return 0
    return checker.run()


if __name__ == "__main__":
    raise SystemExit(main())
