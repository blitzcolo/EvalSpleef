#!/usr/bin/env python3
"""logic-002 隐藏测试（验证侧；不进入 Agent 镜像）。

覆盖：
  T01 生成器自检（双求解器一致、答案规模、确定性）
  T02 折叠/打孔参数范围与升序约束
  T03 Agent 可见 instances.json 与种子重算一致（防篡改）
  T04 两份参考解都通过隐藏比对（正向模拟 / 逆向重建）
  T05 两解答案一致
  T06 空 Agent / 缺文件必须失败
  T07 >=5 个变异体必须被测试杀死
  T08 多组种子下生成器不变量成立
  T09 测试文件与输入哈希锁
  T10 格式违规（排序、重复、越界、推导长度/代码块）必须被拒绝
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
PKG = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import checker  # noqa: E402
import generator  # noqa: E402

VISIBLE_INSTANCES = PKG / "workspace" / "instances.json"
HASH_MANIFEST = HERE / "hashes.json"
REF_A = PKG / "answers" / "reference-a.py"
REF_B = PKG / "answers" / "reference-b.py"


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _RunRefsMixin:
    @classmethod
    def setUpClass(cls) -> None:
        cls.ref_a = _load_module(REF_A, "logic002_ref_a")
        cls.ref_b = _load_module(REF_B, "logic002_ref_b")
        cls.instances = generator.build_instances()
        cls.expected = cls.instances
        cls.public_doc = generator.public_view(cls.instances)
        cls.tmp = tempfile.TemporaryDirectory(prefix="logic002-")
        cls.tmpdir = Path(cls.tmp.name)
        cls.answer_a = cls.tmpdir / "answer-a.json"
        cls.answer_b = cls.tmpdir / "answer-b.json"
        cls.answer_a.write_text(
            json.dumps(cls.ref_a.solve(cls.public_doc), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        cls.answer_b.write_text(
            json.dumps(cls.ref_b.solve(cls.public_doc), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()


class TestGenerator(unittest.TestCase):
    def test_t01_generator_invariants(self) -> None:
        instances = generator.build_instances()
        self.assertEqual(generator.validate(instances), [])
        self.assertEqual(len(instances), len(generator.INSTANCE_SPECS))

    def test_t02_parameters_and_ordering(self) -> None:
        for inst in generator.build_instances():
            self.assertLessEqual(len(inst["folds"]), 4)
            self.assertEqual(len(inst["folds"]), inst["folds"].__len__())
            for p in inst["punches"]:
                self.assertGreaterEqual(p, 1)
                self.assertLessEqual(p, generator.N >> len(inst["folds"]))
            self.assertEqual(inst["holes"], sorted(inst["holes"]))
            self.assertEqual(len(set(inst["holes"])), len(inst["holes"]))
            self.assertEqual(
                len(inst["holes"]), (1 << len(inst["folds"])) * len(inst["punches"])
            )

    def test_t03_visible_instances_untampered(self) -> None:
        on_disk = json.loads(VISIBLE_INSTANCES.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, generator.public_view(generator.build_instances()))
        self.assertNotIn("holes", json.dumps(on_disk))


class TestReferenceSolutions(_RunRefsMixin, unittest.TestCase):
    def test_t04_reference_a_passes(self) -> None:
        self.assertTrue(checker.score(str(self.answer_a), self.expected)["hard_gate_passed"])

    def test_t04_reference_b_passes(self) -> None:
        self.assertTrue(checker.score(str(self.answer_b), self.expected)["hard_gate_passed"])

    def test_t05_references_agree(self) -> None:
        a = json.loads(self.answer_a.read_text(encoding="utf-8"))
        b = json.loads(self.answer_b.read_text(encoding="utf-8"))
        self.assertEqual(
            [i["holes"] for i in a["instances"]], [i["holes"] for i in b["instances"]]
        )


class TestEmptyAndMutants(_RunRefsMixin, unittest.TestCase):
    def _reject(self, doc: dict, label: str) -> None:
        path = self.tmpdir / f"mutant-{label}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(checker.score(str(path), self.expected)["hard_gate_passed"])

    def test_t06_empty_agent_rejected(self) -> None:
        empty = self.tmpdir / "empty.json"
        empty.write_text("", encoding="utf-8")
        self.assertFalse(checker.score(str(empty), self.expected)["hard_gate_passed"])
        self.assertFalse(
            checker.score(str(self.tmpdir / "absent.json"), self.expected)["hard_gate_passed"]
        )

    def test_t07_mutants_killed(self) -> None:
        base = json.loads(self.answer_a.read_text(encoding="utf-8"))
        mutants: list[tuple[str, dict]] = []

        m1 = json.loads(json.dumps(base))
        m1["instances"][1]["holes"].pop()
        mutants.append(("missing-hole", m1))

        m2 = json.loads(json.dumps(base))
        m2["instances"][2]["holes"].append(33)
        mutants.append(("out-of-range", m2))

        m3 = json.loads(json.dumps(base))
        m3["instances"][0]["holes"] = list(reversed(m3["instances"][0]["holes"]))
        mutants.append(("unsorted", m3))

        m4 = json.loads(json.dumps(base))
        m4["instances"][1]["holes"][0] = (
            m4["instances"][1]["holes"][0] % generator.N
        ) + 1
        mutants.append(("wrong-hole", m4))

        m5 = json.loads(json.dumps(base))
        del m5["instances"][3]["derivation"]
        mutants.append(("missing-derivation", m5))

        m6 = json.loads(json.dumps(base))
        m6["instances"][2]["id"] = 42
        mutants.append(("unknown-id", m6))

        m7 = json.loads(json.dumps(base))
        m7["instances"] = m7["instances"][:-1]
        mutants.append(("missing-instance", m7))

        for label, doc in mutants:
            with self.subTest(mutant=label):
                self._reject(doc, label)

    def test_t10_format_violations(self) -> None:
        base = json.loads(self.answer_a.read_text(encoding="utf-8"))
        cases = 0

        prose = self.tmpdir / "prose.json"
        prose.write_text("答案：" + json.dumps(base, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(checker.score(str(prose), self.expected)["hard_gate_passed"])
        cases += 1

        dup = json.loads(json.dumps(base))
        dup["instances"][0]["holes"] = dup["instances"][0]["holes"] + [dup["instances"][0]["holes"][0]]
        p = self.tmpdir / "dup.json"
        p.write_text(json.dumps(dup, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(checker.score(str(p), self.expected)["hard_gate_passed"])
        cases += 1

        long_doc = json.loads(json.dumps(base))
        long_doc["instances"][0]["derivation"] = "很长的推导" * 100
        p = self.tmpdir / "long.json"
        p.write_text(json.dumps(long_doc, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(checker.score(str(p), self.expected)["hard_gate_passed"])
        cases += 1

        code_doc = json.loads(json.dumps(base))
        code_doc["instances"][0]["derivation"] = "看代码" + "```" + "print(1)" + "```"
        p = self.tmpdir / "code.json"
        p.write_text(json.dumps(code_doc, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(checker.score(str(p), self.expected)["hard_gate_passed"])
        cases += 1

        extra = json.loads(json.dumps(base))
        extra["notes"] = "hi"
        p = self.tmpdir / "extra.json"
        p.write_text(json.dumps(extra, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(checker.score(str(p), self.expected)["hard_gate_passed"])
        cases += 1
        self.assertEqual(cases, 5)


class TestRobustness(unittest.TestCase):
    def test_t08_generator_invariants_many_seeds(self) -> None:
        for seed in range(200, 206):
            with self.subTest(seed=seed):
                self.assertEqual(generator.validate(generator.build_instances(seed), seed), [])

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
