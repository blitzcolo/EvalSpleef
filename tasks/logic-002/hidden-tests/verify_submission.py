#!/usr/bin/env python3
"""logic-002 提交验收入口（验证侧）。

硬门槛：
  HG1_holes_correct     4 个实例的 holes 全部正确
  HG2_format_ok         格式合规（纯 JSON / 键集合 / 升序去重 / 推导限制）
  HG3_input_untampered  instances.json 的 sha256 未被改动

用法：python3 verify_submission.py --answer /workspace/answer.json [--json]
退出码：0 = 三闸全过。
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
    parser = argparse.ArgumentParser(description="logic-002 提交验收")
    parser.add_argument("--answer", required=True)
    parser.add_argument("--expected-seed", type=int, default=generator.SEED)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    expected_instances = generator.build_instances(args.expected_seed)
    result = checker.score(args.answer, expected_instances)
    untampered, input_hash = input_hash_ok()

    hg1 = bool(result["all_holes_correct"])
    hg2 = not result["format_violations"]
    hg3 = untampered
    report = {
        "task_id": "logic-002",
        "task_version": "1.0.0",
        "HG1_holes_correct": hg1,
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
        print(f"HG1_holes_correct={hg1} HG2_format_ok={hg2} HG3_input_untampered={hg3}")
        for v in report["violations"]:
            print(f"  violation: {v}")
    return 0 if report["hard_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
