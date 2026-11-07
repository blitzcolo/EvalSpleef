#!/usr/bin/env python3
"""data-001 隐藏测试（验证侧；不进入 Agent 镜像）。

T01 oracle 确定性；T02 样例期望与 oracle 一致；T03 参考解 B 在隐藏数据上通过；
T04 pandas 参考解可编译（无 pandas 时跳过运行）；T05 变异体全部被杀；T06 空提交失败；
T07 容差边界；T08 隐藏数据与样例不同且覆盖 5 类脏行；T09 哈希锁；T10 格式违规；
T11 隐藏数据规模与分布（防退化）；T12 参考解 B 在样例上通过。
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import checker  # noqa: E402
import gen_data  # noqa: E402
import oracle  # noqa: E402

WORKSPACE = PKG / "workspace"
REF_A = PKG / "answers" / "reference-a.py"
REF_B = PKG / "answers" / "reference-b.py"
HASH_MANIFEST = HERE / "hashes.json"


def _run_script(script: Path, log_path: Path, out_path: Path) -> int:
    proc = subprocess.run(
        [sys.executable, str(script), str(log_path), str(out_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode


class Data001Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="data001-")
        cls.tmpdir = Path(cls.tmp.name)
        cls.hidden_lines = gen_data.build_hidden()
        cls.hidden_log = cls.tmpdir / "hidden.log"
        cls.hidden_log.write_text("\n".join(cls.hidden_lines) + "\n", encoding="utf-8")
        cls.expected_hidden = oracle.build_report(cls.hidden_lines)
        cls.ref_b_out = cls.tmpdir / "ref-b-hidden.json"
        assert _run_script(REF_B, cls.hidden_log, cls.ref_b_out) == 0
        cls.sample_expected = json.loads(
            (WORKSPACE / "expected_sample.json").read_text(encoding="utf-8")
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    # ---------------------------------------------------------------- 生成器/口径
    def test_t01_oracle_deterministic(self) -> None:
        again = oracle.build_report(gen_data.build_hidden())
        self.assertEqual(json.dumps(again, sort_keys=True), json.dumps(self.expected_hidden, sort_keys=True))

    def test_t02_sample_expected_matches_oracle(self) -> None:
        lines = (WORKSPACE / "access.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(oracle.build_report(lines), self.sample_expected)

    def test_t08_hidden_differs_and_covers_reasons(self) -> None:
        sample_lines = (WORKSPACE / "access.log").read_text(encoding="utf-8").splitlines()
        self.assertNotEqual(len(sample_lines), len(self.hidden_lines))
        self.assertNotEqual(
            hashlib.sha256("\n".join(sample_lines).encode()).hexdigest(),
            hashlib.sha256("\n".join(self.hidden_lines).encode()).hexdigest(),
        )
        reasons = self.expected_hidden["malformed_by_reason"]
        for reason, count in reasons.items():
            self.assertGreater(count, 0, f"隐藏数据未覆盖脏行类别 {reason}")
        self.assertGreater(self.expected_hidden["total_valid"], 200)
        self.assertGreater(self.expected_hidden["malformed"], 20)

    def test_t11_hidden_distribution(self) -> None:
        report = self.expected_hidden
        self.assertEqual(
            report["total_valid"] + report["malformed"],
            len(self.hidden_lines),
            "有效行 + 脏行 应等于总行数",
        )
        self.assertEqual(
            report["malformed"], sum(report["malformed_by_reason"].values())
        )
        self.assertEqual(len(report["top_paths"]), 5)
        paths = [item["path"] for item in report["top_paths"]]
        self.assertEqual(len(set(paths)), 5)

    # ---------------------------------------------------------------- 参考解
    def test_t03_reference_b_hidden_passes(self) -> None:
        result = checker.score(str(self.ref_b_out), self.expected_hidden)
        self.assertTrue(result["hard_gate_passed"], result["value_mismatches"])

    def test_t12_reference_b_sample_passes(self) -> None:
        out = self.tmpdir / "ref-b-sample.json"
        self.assertEqual(_run_script(REF_B, WORKSPACE / "access.log", out), 0)
        result = checker.score(str(out), self.sample_expected)
        self.assertTrue(result["hard_gate_passed"], result["value_mismatches"])

    def test_t04_pandas_reference(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "py_compile", str(REF_A)], capture_output=True, text=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        try:
            import pandas  # noqa: F401
        except ImportError:
            self.skipTest("本机未安装 pandas：reference-a 只能编译校验，运行验证 TODO(run-on-host)")
        out = self.tmpdir / "ref-a-hidden.json"
        self.assertEqual(_run_script(REF_A, self.hidden_log, out), 0)
        result = checker.score(str(out), self.expected_hidden)
        self.assertTrue(result["hard_gate_passed"], result["value_mismatches"])

    # ---------------------------------------------------------------- 变异体/容差
    def _dump(self, doc: dict, name: str) -> str:
        path = self.tmpdir / f"mutant-{name}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def test_t05_mutants_killed(self) -> None:
        base = json.loads(self.ref_b_out.read_text(encoding="utf-8"))
        mutants: list[tuple[str, dict]] = []

        m = json.loads(json.dumps(base)); m["total_valid"] += 1; mutants.append(("total", m))
        m = json.loads(json.dumps(base)); m["unique_clients"] -= 1; mutants.append(("clients", m))
        m = json.loads(json.dumps(base)); m["malformed"] += 2; mutants.append(("malformed", m))
        m = json.loads(json.dumps(base)); m["malformed_by_reason"]["bad_format"] += 1; mutants.append(("reason", m))
        m = json.loads(json.dumps(base)); m["error_rate"] = round(m["error_rate"] + 0.001, 4); mutants.append(("errrate", m))
        m = json.loads(json.dumps(base)); m["p95_duration_ms"] += 5.0; mutants.append(("p95", m))
        m = json.loads(json.dumps(base)); m["top_paths"] = list(reversed(m["top_paths"])); mutants.append(("order", m))
        m = json.loads(json.dumps(base)); m["top_paths"][0]["count"] += 1; mutants.append(("toppath", m))
        m = json.loads(json.dumps(base)); m["notes"] = "hi"; mutants.append(("extra", m))
        m = json.loads(json.dumps(base)); del m["p95_duration_ms"]; mutants.append(("missing", m))
        m = json.loads(json.dumps(base)); m["total_valid"] = str(m["total_valid"]); mutants.append(("type", m))
        for label, doc in mutants:
            with self.subTest(mutant=label):
                self.assertFalse(
                    checker.score(self._dump(doc, label), self.expected_hidden)["hard_gate_passed"],
                    f"变异体未杀死：{label}",
                )

    def test_t06_empty_agent_rejected(self) -> None:
        empty = self.tmpdir / "empty.json"
        empty.write_text("", encoding="utf-8")
        self.assertFalse(checker.score(str(empty), self.expected_hidden)["hard_gate_passed"])
        self.assertFalse(
            checker.score(str(self.tmpdir / "absent.json"), self.expected_hidden)["hard_gate_passed"]
        )

    def test_t07_tolerance_boundaries(self) -> None:
        base = json.loads(self.ref_b_out.read_text(encoding="utf-8"))
        # error_rate 容差 1e-4：5e-5 应通过，2e-4 应失败
        near = json.loads(json.dumps(base)); near["error_rate"] = base["error_rate"] + 5e-5
        self.assertTrue(checker.score(self._dump(near, "err-near"), self.expected_hidden)["hard_gate_passed"])
        far = json.loads(json.dumps(base)); far["error_rate"] = base["error_rate"] + 2e-4
        self.assertFalse(checker.score(self._dump(far, "err-far"), self.expected_hidden)["hard_gate_passed"])
        # p95 容差 1.0ms：0.5 应通过，2.0 应失败
        near = json.loads(json.dumps(base)); near["p95_duration_ms"] = base["p95_duration_ms"] + 0.5
        self.assertTrue(checker.score(self._dump(near, "p95-near"), self.expected_hidden)["hard_gate_passed"])
        far = json.loads(json.dumps(base)); far["p95_duration_ms"] = base["p95_duration_ms"] + 2.0
        self.assertFalse(checker.score(self._dump(far, "p95-far"), self.expected_hidden)["hard_gate_passed"])

    def test_t10_format_violations(self) -> None:
        base = json.loads(self.ref_b_out.read_text(encoding="utf-8"))
        prose = self.tmpdir / "prose.json"
        prose.write_text("报告如下：" + json.dumps(base, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(checker.score(str(prose), self.expected_hidden)["hard_gate_passed"])
        unsorted = json.loads(json.dumps(base))
        unsorted["top_paths"] = [
            {"path": "/api/zzz", "count": 1}
        ] + unsorted["top_paths"][:4]
        self.assertFalse(
            checker.score(self._dump(unsorted, "sort"), self.expected_hidden)["hard_gate_passed"]
        )
        badkeys = json.loads(json.dumps(base))
        badkeys["malformed_by_reason"] = {"bad_format": 1}
        self.assertFalse(
            checker.score(self._dump(badkeys, "reasons"), self.expected_hidden)["hard_gate_passed"]
        )

    def test_t09_hash_lock(self) -> None:
        manifest = json.loads(HASH_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(manifest["algorithm"], "sha256")
        for rel, expected_hash in manifest["locked_files"].items():
            target = (HERE / rel).resolve()
            self.assertTrue(target.is_file(), f"哈希锁文件缺失：{rel}")
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            self.assertEqual(actual, expected_hash, f"哈希不符（测试被改动？）：{rel}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
