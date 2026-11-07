#!/usr/bin/env python3
"""debug-001 变异测试：把 5 处缺陷重新注入参考解，隐藏测试必须全部失败。

用法：
    python3 mutate.py <已修复的工程目录>

退出码：0=全部变异体被杀死；1=存在存活变异体；2=用法错误。
"""

import os
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests")

MUTATIONS = [
    (
        "M1-truncate-rounding",
        "ledgerq/money.py",
        "    return value.quantize(CENT, rounding=ROUND_HALF_UP)",
        "    return Decimal(int(float(value) * 100)) / 100",
        "D1",
    ),
    (
        "M2-inclusive-end",
        "ledgerq/dates.py",
        "    if end is not None and value >= end:\n        return False",
        "    if end is not None and value > end:\n        return False",
        "D2",
    ),
    (
        "M3-swallow-parse-error",
        "ledgerq/pipeline.py",
        '            except (KeyError, ValueError, ArithmeticError) as exc:\n                raise LedgerError(f"无法解析的行: {exc}", line_no) from exc',
        "            except (KeyError, ValueError, ArithmeticError):\n                continue",
        "D3",
    ),
    (
        "M4-mutable-default",
        "ledgerq/pipeline.py",
        'def group_by_month(rows, acc=None):\n    """按日历月分组；返回 {"YYYY-MM": [row, ...]}。"""\n    if acc is None:\n        acc = {}\n',
        'def group_by_month(rows, acc={}):\n    """按日历月分组；返回 {"YYYY-MM": [row, ...]}。"""\n',
        "D4",
    ),
    (
        "M5-mix-currencies",
        "ledgerq/pipeline.py",
        "        for currency in sorted(sums):\n            totals[currency] = format_money(sums[currency])",
        '        single = sorted(sums)[0]\n        totals[single] = format_money(sum(sums.values(), Decimal("0")))',
        "D5",
    ),
]


def run_suite(tree: str) -> int:
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["LEDGER_CANDIDATE"] = tree
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", TESTS_DIR, "-p", "test_*.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.returncode


def main(argv) -> int:
    if len(argv) != 2:
        print("用法: mutate.py <已修复的工程目录>", file=sys.stderr)
        return 2
    tree = os.path.abspath(argv[1])
    if not os.path.isdir(tree):
        print(f"ERROR: 目录不存在: {tree}", file=sys.stderr)
        return 2
    survived = []
    print(f"{'变异体':<26}{'缺陷':<6}{'结果':<8}")
    with tempfile.TemporaryDirectory() as tmp:
        for index, (name, relative, old, new, defect) in enumerate(MUTATIONS, start=1):
            victim = os.path.join(tmp, f"m{index}")
            shutil.copytree(tree, victim, ignore=shutil.ignore_patterns("__pycache__"))
            target = os.path.join(victim, relative)
            with open(target, encoding="utf-8") as handle:
                source = handle.read()
            if old not in source:
                print(f"{name:<26}{defect:<6}ERROR（未找到待替换片段）")
                survived.append(name)
                continue
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(source.replace(old, new, 1))
            code = run_suite(victim)
            killed = code != 0
            print(f"{name:<26}{defect:<6}{'killed' if killed else 'SURVIVED'}")
            if not killed:
                survived.append(name)
    if survived:
        print(f"失败：{len(survived)} 个变异体存活: {', '.join(survived)}", file=sys.stderr)
        return 1
    print(f"OK：{len(MUTATIONS)}/{len(MUTATIONS)} 变异体被杀死")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
