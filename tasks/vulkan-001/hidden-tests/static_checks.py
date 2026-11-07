#!/usr/bin/env python3
"""vulkan-001 静态检查：工程结构 / 验证层 / 阴影资源 / 截图导出 / 着色器接线。

退出码：0=通过；1=检查失败；2=用法/环境错误；3=契约文件被修改（硬门槛）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys

LOCKS_FILE = pathlib.Path(__file__).with_name("locks.json")


class Checker:
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.failures: list[str] = []
        self.lock_tampered = False

    def read(self, rel: str) -> str:
        p = self.root / rel
        if not p.is_file():
            self.fail(f"缺少文件：{rel}")
            return ""
        return p.read_text(encoding="utf-8", errors="replace")

    def fail(self, msg: str) -> None:
        self.failures.append(msg)

    def require(self, rel: str, needles: list[str], label: str) -> None:
        text = self.read(rel)
        for n in needles:
            if n not in text:
                self.fail(f"{label} 缺少：{n}")

    def check_structure(self) -> None:
        for rel in [
            "CMakeLists.txt",
            "src/main.cpp",
            "src/shadow_math.cpp",
            "src/shadow_math.h",
            "shaders/scene.vert",
            "shaders/scene.frag",
            "shaders/shadow.vert",
            "shaders/shadow.frag",
        ]:
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
            if hashlib.sha256(p.read_bytes()).hexdigest() != want:
                self.lock_tampered = True
                self.fail(f"契约文件被修改：{rel}（原样保留，不得改动）")

    def check_main(self) -> None:
        text = self.read("src/main.cpp")
        for n in [
            "VK_LAYER_KHRONOS_validation",
            "vkCreateDebugUtilsMessengerEXT",
            "VK_FORMAT_D32_SFLOAT",
            "VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT",
            "VK_IMAGE_USAGE_SAMPLED_BIT",
        ]:
            if n not in text:
                self.fail(f"main.cpp 缺少：{n}")
        if "P6" not in text:
            self.fail("main.cpp 没有实现 P6 PPM 截图导出")
        if "TODO(" in text:
            self.fail("main.cpp 仍留有未完成的 TODO 标记")
        if text.count("vkCmdBeginRenderPass") < 1:
            self.fail("main.cpp 没有录制渲染通道")

    def check_math(self) -> None:
        text = self.read("src/shadow_math.cpp")
        if "TODO:" in text:
            self.fail("shadow_math.cpp 仍是空壳（含 TODO）")
        if "vk" in text.lower() and "vulkan" in text.lower():
            self.fail("shadow_math.cpp 不应依赖 Vulkan（必须可单独 host 编译）")

    def check_frag(self) -> None:
        text = self.read("shaders/scene.frag")
        if "u_shadow_map" not in text:
            self.fail("scene.frag 缺少：u_shadow_map")
        if "texture(" not in text and "textureOffset(" not in text:
            self.fail("scene.frag 缺少：texture( 或 textureOffset(")
        if "shadow" not in text.lower():
            self.fail("scene.frag 没有使用阴影因子")
        if "for (" not in text and "textureOffset" not in text:
            self.fail("scene.frag 没有实现 3x3 PCF（for 循环或 textureOffset）")
        if "TODO" in text:
            self.fail("scene.frag 仍留有 TODO")

    def check_shadow_shaders(self) -> None:
        vert = self.read("shaders/shadow.vert")
        if "gl_Position" not in vert:
            self.fail("shadow.vert 没有写 gl_Position")
        frag = self.read("shaders/shadow.frag")
        if not frag.strip():
            self.fail("shadow.frag 为空")

    def check_cmake(self) -> None:
        text = self.read("CMakeLists.txt")
        for n in ["shadow_math.cpp", "glslc", "scene.frag", "shadow.vert"]:
            if n not in text:
                self.fail(f"CMakeLists.txt 缺少：{n}")

    def run(self) -> int:
        self.check_structure()
        self.check_locks()
        self.check_main()
        self.check_math()
        self.check_frag()
        self.check_shadow_shaders()
        self.check_cmake()
        if self.lock_tampered:
            print("FAIL: 契约文件被修改（硬门槛 HG3）", file=sys.stderr)
            return 3
        if self.failures:
            for f in self.failures:
                print(f"FAIL: {f}", file=sys.stderr)
            return 1
        print("PASS: vulkan-001 静态检查通过")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", nargs="?", default=str(pathlib.Path(__file__).parent.parent / "starter"))
    ap.add_argument("--locks-only", action="store_true")
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
