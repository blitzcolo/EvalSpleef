#!/usr/bin/env python3
"""仓库一致性自检：目录契约、相对链接、Schema 引用、可运行测试。

用法：
    python3 scripts/check_repo.py            # 结构 + 链接 + Schema 检查
    python3 scripts/check_repo.py --run      # 额外运行参考实现单测与题包验收脚本

退出码：0 = 全部通过；1 = 有 error；2 = 仅 warning。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ERRORS: list[str] = []
WARNINGS: list[str] = []

REQUIRED_DOCS = [
    "docs/00-conventions.md",
    "docs/01-architecture.md",
    "docs/02-eval-flow.md",
    "docs/03-grading.md",
    "docs/04-isolation.md",
    "docs/05-task-library.md",
    "docs/06-answer-and-acceptance.md",
    "docs/07-harness-mapping.md",
    "docs/08-implementation-notes.md",
    "README.md",
]

REQUIRED_SCHEMAS = [
    "schemas/task.schema.json",
    "schemas/rubric.schema.json",
    "schemas/defect.schema.json",
    "schemas/feedback.schema.json",
    "schemas/grade.schema.json",
    "schemas/trial.schema.json",
]

TASK_PACKAGE_FILES = [
    "task.yaml",
    "instruction.md",
    "persona.md",
    "rubric.yaml",
    "defects.yaml",
    "answers/empty-agent.md",
    "hidden-tests/run.sh",
    "canary.md",
    "notes.md",
]

LINK_RE = re.compile(r"(?<!\!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")


def err(msg: str) -> None:
    ERRORS.append(msg)


def warn(msg: str) -> None:
    WARNINGS.append(msg)


def check_required_files() -> None:
    for rel in REQUIRED_DOCS:
        if not (ROOT / rel).is_file():
            err(f"缺少必需文档：{rel}")
    for rel in REQUIRED_SCHEMAS:
        p = ROOT / rel
        if not p.is_file():
            err(f"缺少必需 Schema：{rel}")
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            err(f"Schema 不是合法 JSON：{rel} ({exc})")
            continue
        expected = f"https://llm-eval.local/schemas/{p.name}"
        if data.get("$id") != expected:
            err(f"Schema $id 不符：{rel} 期望 {expected}，实际 {data.get('$id')}")
        for key in ("$schema", "title", "type", "properties"):
            if key not in data:
                warn(f"Schema 缺字段 {key}：{rel}")
        # 检查跨文件 $ref 是否存在
        for ref in find_refs(data):
            if ref.startswith("#") or "://" in ref:
                continue
            target = (p.parent / ref.split("#")[0]).resolve()
            if not target.is_file():
                err(f"Schema 引用不存在：{rel} -> {ref}")


def find_refs(node: object) -> list[str]:
    refs: list[str] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "$ref" and isinstance(v, str):
                refs.append(v)
            else:
                refs.extend(find_refs(v))
    elif isinstance(node, list):
        for item in node:
            refs.extend(find_refs(item))
    return refs


def check_task_packages() -> None:
    base = ROOT / "appendix" / "examples"
    if not base.is_dir():
        err("缺少 appendix/examples/ 目录")
        return
    packages = sorted(p for p in base.iterdir() if p.is_dir())
    if not packages:
        err("appendix/examples/ 下没有任何题包")
    for pkg in packages:
        for rel in TASK_PACKAGE_FILES:
            if not (pkg / rel).is_file():
                err(f"题包缺文件：{pkg.name}/{rel}")
        for letter in ("a", "b"):
            found = any((pkg / "answers").glob(f"reference-{letter}.*"))
            if not found:
                err(f"题包缺参考解：{pkg.name}/answers/reference-{letter}.*")
        # 隐藏测试不得出现在 Agent 可见文件里（task.yaml 属控制面元数据，硬门槛引用 hidden-tests 是正常的）
        for visible in ("instruction.md",):
            f = pkg / visible
            if f.is_file() and "hidden-tests" in f.read_text(encoding="utf-8"):
                warn(f"{pkg.name}/{visible} 提及 hidden-tests，检查是否泄露")
        ws = pkg / "workspace"
        if ws.is_dir():
            for f in ws.rglob("*"):
                if f.is_file() and "hidden-tests" in str(f):
                    warn(f"{pkg.name}/workspace 中出现 hidden-tests 路径：{f.relative_to(pkg)}")


def check_links() -> None:
    for md in sorted(ROOT.rglob("*.md")):
        if ".git" in md.parts or "reference/examples" in md.as_posix():
            continue
        text = md.read_text(encoding="utf-8", errors="replace")
        for m in LINK_RE.finditer(text):
            target = m.group(2).split("#")[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            if target.startswith("/"):
                err(f"绝对路径链接应改相对路径：{md.relative_to(ROOT)} -> {m.group(2)}")
                continue
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                err(f"链接失效：{md.relative_to(ROOT)} -> {m.group(2)}")


def check_enums() -> None:
    """粗查关键枚举拼写是否统一（防止 B+/C+ 写成 Bplus 之类）。"""
    tiers = {"P", "A", "B+", "B", "C+", "C", "D", "F"}
    for schema in ("schemas/grade.schema.json", "schemas/trial.schema.json"):
        p = ROOT / schema
        if not p.is_file():
            continue
        data = json.loads(p.read_text(encoding="utf-8"))
        found = set()

        def walk(node: object, key: str = "", bucket: set[str] = found) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "enum" and isinstance(v, list) and set(v) >= tiers - {"P", "F"}:
                        bucket.update(x for x in v if x in tiers)
                    walk(v, k)
            elif isinstance(node, list):
                for i in node:
                    walk(i, key)

        walk(data)
        if found and found != tiers:
            warn(f"{schema} 中档位枚举不完整：{sorted(found)}")


def run_tests() -> None:
    ref = ROOT / "reference"
    if (ref / "tests").is_dir():
        cmd = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            str(ref / "tests"),
            "-t",
            str(ref),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
        print(f"[run] 参考实现单测：exit={proc.returncode} | " + " / ".join(tail))
        if proc.returncode != 0:
            err("参考实现单测未通过")
    base = ROOT / "appendix" / "examples"
    if base.is_dir():
        for pkg in sorted(p for p in base.iterdir() if p.is_dir()):
            runner = pkg / "hidden-tests" / "run.sh"
            if runner.is_file() and os.access(runner, os.X_OK):
                # run.sh 语义：默认跑候选工程（Agent 产物）；--selftest 验证题包自身
                # （参考解通过 / starter 失败 / 哈希锁 / 变异测试）。仓检查用 --selftest。
                cmd = ["bash", str(runner)]
                try:
                    has_selftest = "--selftest" in runner.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    has_selftest = False
                if has_selftest:
                    cmd.append("--selftest")
                try:
                    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=pkg, timeout=300)
                except subprocess.TimeoutExpired:
                    err(f"题包隐藏测试超时：{pkg.name}")
                    continue
                print(
                    f"[run] {pkg.name} hidden-tests{' --selftest' if has_selftest else ''}：exit={proc.returncode}"
                )
                if proc.returncode != 0:
                    tail = (proc.stdout + proc.stderr).strip().splitlines()[-3:]
                    err(f"题包隐藏测试未通过：{pkg.name}（{' / '.join(tail)}）")
            else:
                warn(f"题包隐藏测试脚本不可执行或不存在：{pkg.name}")


def check_todos() -> None:
    counts = {"TODO(verify)": 0, "TODO(run-on-host)": 0, "TODO": 0}
    for md in ROOT.rglob("*.md"):
        text = md.read_text(encoding="utf-8", errors="replace")
        if "TODO(verify)" in text:
            counts["TODO(verify)"] += text.count("TODO(verify)")
        if "TODO(run-on-host)" in text:
            counts["TODO(run-on-host)"] += text.count("TODO(run-on-host)")
        counts["TODO"] += text.count("TODO(")
    print(
        f"[info] TODO(verify)={counts['TODO(verify)']} TODO(run-on-host)={counts['TODO(run-on-host)']}"
    )
    if counts["TODO(verify)"] > 20:
        warn("TODO(verify) 超过 20 处，证据链不足")


def main() -> int:
    parser = argparse.ArgumentParser(description="llm-eval 仓库自检")
    parser.add_argument("--run", action="store_true", help="运行参考实现单测与题包验收脚本")
    args = parser.parse_args()

    check_required_files()
    check_task_packages()
    check_links()
    check_enums()
    check_todos()
    if args.run:
        run_tests()

    for w in WARNINGS:
        print(f"[WARN] {w}")
    for e in ERRORS:
        print(f"[ERROR] {e}")
    print(f"\n结果：{len(ERRORS)} error / {len(WARNINGS)} warning")
    if ERRORS:
        return 1
    return 0 if not WARNINGS else 2


if __name__ == "__main__":
    raise SystemExit(main())
