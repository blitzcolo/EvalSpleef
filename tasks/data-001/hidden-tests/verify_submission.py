#!/usr/bin/env python3
"""data-001 提交验收入口（验证侧）。

提交物：Agent 交付的清洗脚本（默认 /workspace/clean_logs.py），
接口固定：python3 clean_logs.py <input.log> <output.json>。

硬门槛：
  HG1_sample_ok     在 Agent 可见样例上运行脚本，报告与 expected_sample.json 一致（含容差）
  HG2_hidden_ok     在验证侧隐藏日志上运行脚本，报告与 oracle 一致（含容差）
  HG3_integrity_ok  工作区输入文件哈希未变；提交脚本存在且能在超时内退出 0

用法：
    python3 verify_submission.py --submission /workspace/clean_logs.py
退出码：0 = 三闸全过。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import checker  # noqa: E402
import gen_data  # noqa: E402
import oracle  # noqa: E402

WORKSPACE = PKG / "workspace"
HASH_MANIFEST = HERE / "hashes.json"
RUN_TIMEOUT_S = 120


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_script(submission: Path, log_path: Path, out_path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(submission), str(log_path), str(out_path)],
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT_S,
    )
    return {
        "returncode": proc.returncode,
        "stderr_tail": proc.stderr.strip().splitlines()[-3:],
        "stdout_tail": proc.stdout.strip().splitlines()[-3:],
    }


def _integrity() -> tuple[bool, dict]:
    manifest = json.loads(HASH_MANIFEST.read_text(encoding="utf-8"))
    details: dict = {}
    ok = True
    for rel in ("access.log", "expected_sample.json"):
        expected = manifest["locked_files"].get(f"../workspace/{rel}")
        actual = _sha256(WORKSPACE / rel)
        details[rel] = {"ok": actual == expected, "sha256": actual}
        ok = ok and actual == expected
    return ok, details


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="data-001 提交验收")
    parser.add_argument("--submission", default="/workspace/clean_logs.py")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    submission = Path(args.submission)
    report: dict = {
        "task_id": "data-001",
        "task_version": "1.0.0",
        "submission": str(submission),
        "HG1_sample_ok": False,
        "HG2_hidden_ok": False,
        "HG3_integrity_ok": False,
        "hard_gate_passed": False,
        "tier_hint": "F",
        "details": {},
    }
    if not submission.is_file():
        report["details"]["submission_missing"] = True
    else:
        with tempfile.TemporaryDirectory(prefix="data001-verify-") as tmp:
            tmpdir = Path(tmp)
            # ---- HG1：可见样例 ----
            out1 = tmpdir / "report-sample.json"
            run1 = _run_script(submission, WORKSPACE / "access.log", out1)
            expected_sample = json.loads(
                (WORKSPACE / "expected_sample.json").read_text(encoding="utf-8")
            )
            score1 = (
                checker.score(str(out1), expected_sample)
                if run1["returncode"] == 0 and out1.is_file()
                else {"hard_gate_passed": False, "format_violations": [{"code": "SCRIPT_FAILED"}]}
            )
            # ---- HG2：隐藏日志 ----
            hidden_lines = gen_data.build_hidden()
            hidden_log = tmpdir / "hidden.log"
            hidden_log.write_text("\n".join(hidden_lines) + "\n", encoding="utf-8")
            expected_hidden = oracle.build_report(hidden_lines)
            out2 = tmpdir / "report-hidden.json"
            run2 = _run_script(submission, hidden_log, out2)
            score2 = (
                checker.score(str(out2), expected_hidden)
                if run2["returncode"] == 0 and out2.is_file()
                else {"hard_gate_passed": False, "format_violations": [{"code": "SCRIPT_FAILED"}]}
            )
            integrity_ok, integrity_details = _integrity()
            report["HG1_sample_ok"] = bool(score1["hard_gate_passed"])
            report["HG2_hidden_ok"] = bool(score2["hard_gate_passed"])
            report["HG3_integrity_ok"] = integrity_ok
            report["details"] = {
                "sample_run": run1,
                "hidden_run": run2,
                "sample_check": score1,
                "hidden_check": score2,
                "integrity": integrity_details,
            }
    report["hard_gate_passed"] = (
        report["HG1_sample_ok"] and report["HG2_hidden_ok"] and report["HG3_integrity_ok"]
    )
    report["tier_hint"] = "P" if report["hard_gate_passed"] else "F"
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"HG1_sample_ok={report['HG1_sample_ok']} "
            f"HG2_hidden_ok={report['HG2_hidden_ok']} "
            f"HG3_integrity_ok={report['HG3_integrity_ok']}"
        )
    return 0 if report["hard_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
