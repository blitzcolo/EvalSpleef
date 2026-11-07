#!/usr/bin/env python3
"""logic-001 隐藏测试（验证侧；不进入 Agent 镜像）。

覆盖：
  T01 生成器自检（唯一性、最小充分记录、确定性）
  T02 规则族规模与拼写
  T03 Agent 可见 instances.json 与种子重算结果一致（防篡改）
  T04 两份参考解都通过隐藏比对（reference-a 穷举 / reference-b 约束传播）
  T05 两份参考解的答案一致（除推导文本外）
  T06 空 Agent / 缺文件必须失败
  T07 >=5 个变异体必须被测试判定杀死
  T08 任意种子下生成器不变量成立（防退化）
  T09 测试文件哈希锁；instances.json 哈希锁
  T10 格式违规（纯 JSON、键集合、推导长度/句数/代码块）必须被拒绝
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
sys_path = str(HERE)
if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

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
        cls.ref_a = _load_module(REF_A, "logic001_ref_a")
        cls.ref_b = _load_module(REF_B, "logic001_ref_b")
        cls.instances = generator.build_instances()
        cls.expected = cls.instances
        cls.public_doc = generator.public_view(cls.instances)
        cls.tmp = tempfile.TemporaryDirectory(prefix="logic001-")
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
        self.assertEqual(generator.FAMILY_SIZE, 320)
        self.assertEqual(generator.validate(generator.build_instances()), [])

    def test_t02_rule_family_spelling(self) -> None:
        self.assertEqual(
            sorted(generator.PERMUTATIONS),
            sorted(
                [
                    "id",
                    "rev",
                    "rot_l1",
                    "rot_r1",
                    "swap_halves",
                    "rot_halves",
                    "evens_first",
                    "rev_halves",
                ]
            ),
        )
        self.assertEqual(tuple(generator.A_VALUES), (1, 3, 7, 9))

    def test_t03_visible_instances_untampered(self) -> None:
        on_disk = json.loads(VISIBLE_INSTANCES.read_text(encoding="utf-8"))
        self.assertEqual(on_disk, generator.public_view(generator.build_instances()))
        blob = json.dumps(on_disk)
        self.assertNotIn('"rule"', blob)
        self.assertNotIn('"next"', blob)


class TestReferenceSolutions(_RunRefsMixin, unittest.TestCase):
    def test_t04_reference_a_passes(self) -> None:
        result = checker.score(str(self.answer_a), self.expected)
        self.assertTrue(result["hard_gate_passed"], result["format_violations"])

    def test_t04_reference_b_passes(self) -> None:
        result = checker.score(str(self.answer_b), self.expected)
        self.assertTrue(result["hard_gate_passed"], result["format_violations"])

    def test_t05_references_agree_on_answers(self) -> None:
        a = json.loads(self.answer_a.read_text(encoding="utf-8"))
        b = json.loads(self.answer_b.read_text(encoding="utf-8"))

        def strip(doc: dict) -> list[dict]:
            return [{"id": i["id"], "rule": i["rule"], "next": i["next"]} for i in doc["instances"]]

        self.assertEqual(strip(a), strip(b))


class TestEmptyAndMutants(_RunRefsMixin, unittest.TestCase):
    def _assert_rejected(self, doc: dict, label: str) -> None:
        path = self.tmpdir / f"mutant-{label}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        result = checker.score(str(path), self.expected)
        self.assertFalse(result["hard_gate_passed"], f"变异体未被杀死：{label}")

    def test_t06_empty_agent_rejected(self) -> None:
        empty = self.tmpdir / "empty.json"
        empty.write_text("", encoding="utf-8")
        self.assertFalse(checker.score(str(empty), self.expected)["hard_gate_passed"])
        missing = self.tmpdir / "not-exist.json"
        self.assertFalse(checker.score(str(missing), self.expected)["hard_gate_passed"])

    def test_t07_mutants_killed(self) -> None:
        base = json.loads(self.answer_a.read_text(encoding="utf-8"))
        mutants: list[tuple[str, dict]] = []

        m1 = json.loads(json.dumps(base))
        m1["instances"][2]["next"][0] = (m1["instances"][2]["next"][0] + 1) % 10
        mutants.append(("next-digit", m1))

        m2 = json.loads(json.dumps(base))
        m2["instances"][1]["rule"]["perm"] = "rot_r1"
        mutants.append(("wrong-perm", m2))

        m3 = json.loads(json.dumps(base))
        m3["instances"][0]["rule"]["a"] = 3
        mutants.append(("wrong-a", m3))

        m4 = json.loads(json.dumps(base))
        del m4["instances"][3]["derivation"]
        mutants.append(("missing-derivation", m4))

        m5 = json.loads(json.dumps(base))
        mutants.append(("missing-instance", {"instances": m5["instances"][:-1]}))

        m6 = json.loads(json.dumps(base))
        m6["instances"][4]["id"] = 99
        mutants.append(("unknown-id", m6))

        m7 = json.loads(json.dumps(base))
        m7["instances"].append(json.loads(json.dumps(base["instances"][0])))
        mutants.append(("duplicate-id", m7))

        for label, doc in mutants:
            with self.subTest(mutant=label):
                self._assert_rejected(doc, label)

    def test_t10_format_violations(self) -> None:
        base = json.loads(self.answer_a.read_text(encoding="utf-8"))
        prose = self.tmpdir / "prose.json"
        prose.write_text("这是我的答案：" + json.dumps(base, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(checker.score(str(prose), self.expected)["hard_gate_passed"])

        long_doc = json.loads(json.dumps(base))
        long_doc["instances"][0]["derivation"] = "很长的推导" * 100
        self.assertFalse(checker.score(self._dump(long_doc, "long"), self.expected)["hard_gate_passed"])

        code_doc = json.loads(json.dumps(base))
        code_doc["instances"][0]["derivation"] = "看代码" + "```" + "print(1)" + "```"
        self.assertFalse(checker.score(self._dump(code_doc, "code"), self.expected)["hard_gate_passed"])

        extra_doc = json.loads(json.dumps(base))
        extra_doc["notes"] = "hi"
        self.assertFalse(checker.score(self._dump(extra_doc, "extra"), self.expected)["hard_gate_passed"])

        bad_perm = json.loads(json.dumps(base))
        bad_perm["instances"][0]["rule"]["perm"] = "rotate"
        self.assertFalse(checker.score(self._dump(bad_perm, "badperm"), self.expected)["hard_gate_passed"])

    def _dump(self, doc: dict, name: str) -> str:
        path = self.tmpdir / f"format-{name}.json"
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return str(path)


class TestRobustness(unittest.TestCase):
    def test_t08_generator_invariants_many_seeds(self) -> None:
        for seed in range(120, 126):
            with self.subTest(seed=seed):
                self.assertEqual(
                    generator.validate(generator.build_instances(seed), seed), []
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
