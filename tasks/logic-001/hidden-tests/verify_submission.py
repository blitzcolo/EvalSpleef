#!/usr/bin/env python3
"""logic-001 提交验收入口（验证侧；只读 Agent 产物 + 受保护输入哈希）。

三个硬门槛：
  HG1_answer_correct   5 个实例的 rule 与 next 全部正确
  HG2_format_ok        答案格式合规（纯 JSON / 键集合 / 枚举范围 / 推导限制）
  HG3_input_untampered 工作区 instances.json 的 sha256 未被改动

用法：
    python3 verify_submission.py --answer /workspace/answer.json
    python3 verify_submission.py --answer /workspace/answer.json --json
退出码：0 = 三个硬门槛全过；1 = 至少一个失败；2 = 参数/环境错误。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import checker  # noqa: E402
import generator  # noqa: E402

VISIBLE = PKG / "workspace" / "instances.json"
HASH_MANIFEST = HERE / "hashes.json"


def input_hash_ok() -> tuple[bool, str]:
    manifest = json.loads(HASH_MANIFEST.read_text(encoding="utf-8"))
    expected = manifest["locked_files"].get("../workspace/instances.json")
    if expected is None:
        raise RuntimeError("hashes.json 缺少 ../workspace/instances.json 锁定项")
    actual = hashlib.sha256(VISIBLE.read_bytes()).hexdigest()
    return actual == expected, actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="logic-001 提交验收")
    parser.add_argument("--answer", required=True, help="Agent 提交的 answer.json 路径")
    parser.add_argument("--expected-seed", type=int, default=generator.HIDDEN_SEED)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = parser.parse_args(argv)

    expected_instances = generator.build_instances(args.expected_seed)
    result = checker.score(args.answer, expected_instances)
    untampered, input_hash = input_hash_ok()

    hg1 = bool(result["all_rules_correct"] and result["all_next_correct"])
    hg2 = not result["format_violations"]
    hg3 = untampered
    report = {
        "task_id": "logic-001",
        "task_version": "1.0.0",
        "HG1_answer_correct": hg1,
        "HG2_format_ok": hg2,
        "HG3_input_untampered": hg3,
        "hard_gate_passed": hg1 and hg2 and hg3,
        "tier_hint": "P" if (hg1 and hg2 and hg3) else "F",
        "input_sha256": input_hash,
        "violations": result["format_violations"],
        "instances": result["instances"],
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"HG1_answer_correct={hg1} HG2_format_ok={hg2} HG3_input_untampered={hg3}")
        for v in report["violations"]:
            print(f"  violation: {v}")
    return 0 if report["hard_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
