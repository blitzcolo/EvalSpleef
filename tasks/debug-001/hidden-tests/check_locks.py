#!/usr/bin/env python3
"""debug-001 测试文件哈希锁校验（硬门槛 H2）。

用法：
    python3 check_locks.py <locks.sha256> <候选工程根目录>

清单每行格式：sha256  相对路径（相对候选工程根目录），# 开头为注释。
退出码：0=全部匹配；3=文件缺失或被修改；2=清单/参数错误。
"""

import hashlib
import os
import sys


def sha256_of(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_manifest(path: str) -> list[tuple[str, str]]:
    entries = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split()
            if len(parts) != 2:
                raise SystemExit(f"ERROR: 清单行格式错误: {line!r}")
            entries.append((parts[0].lower(), parts[1]))
    if not entries:
        raise SystemExit("ERROR: 哈希清单为空")
    return entries


def main(argv) -> int:
    if len(argv) != 3:
        print("用法: check_locks.py <locks.sha256> <候选工程根目录>", file=sys.stderr)
        return 2
    manifest, candidate = argv[1], argv[2]
    if not os.path.isdir(candidate):
        print(f"ERROR: 候选目录不存在: {candidate}", file=sys.stderr)
        return 2
    violations = []
    for expected, relative in read_manifest(manifest):
        target = os.path.join(candidate, relative)
        if not os.path.isfile(target):
            violations.append(f"缺失: {relative}")
            continue
        actual = sha256_of(target)
        if actual != expected:
            violations.append(f"被修改: {relative}（期望 {expected[:12]}…，实际 {actual[:12]}…）")
    if violations:
        print("LOCK VIOLATION: 测试文件哈希校验失败", file=sys.stderr)
        for item in violations:
            print(f"  - {item}", file=sys.stderr)
        return 3
    print(f"OK: {len(read_manifest(manifest))} 个测试文件哈希未变")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
