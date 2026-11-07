"""可见测试（哈希锁定）：ParticleBench 可构建并输出契约要求的 JSON。"""

import json
import os
import subprocess
import sys
import unittest

START = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BINARY = os.path.join(START, "build", "ParticleBench")


def run_binary(*args):
    proc = subprocess.run([BINARY, *args], cwd=START, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise AssertionError(f"ParticleBench 退出码 {proc.returncode}: {proc.stderr[-800:]}")
    for line in proc.stdout.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line)
    raise AssertionError(f"stdout 没有 JSON 行: {proc.stdout[-500:]}")


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(["make", "bench"], cwd=START, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            raise unittest.SkipTest(f"构建失败（需 macOS + swiftc）: {proc.stderr[-500:]}")

    def test_json_contract(self):
        payload = run_binary("--frames", "3", "--particles", "500", "--width", "320", "--height", "180")
        for key in ("device", "mode", "particles", "width", "height", "frames",
                    "avg_frame_ms", "fps", "non_background_pixels", "frame_hash"):
            self.assertIn(key, payload)
        self.assertEqual(payload["width"], 320)
        self.assertEqual(payload["frames"], 3)
        self.assertGreater(payload["avg_frame_ms"], 0)
        self.assertGreater(payload["non_background_pixels"], 0)

    def test_render_is_deterministic(self):
        args = ("--frames", "3", "--particles", "500", "--width", "320", "--height", "180")
        self.assertEqual(run_binary(*args)["frame_hash"], run_binary(*args)["frame_hash"])


if __name__ == "__main__":
    unittest.main()
