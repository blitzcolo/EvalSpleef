"""可见测试（哈希锁定）：检查 intro.py 的三个入口仍可用、单帧输出是 PPM。"""

import os
import struct
import subprocess
import sys
import tempfile
import unittest

START = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(*args):
    return subprocess.run(
        [sys.executable, "intro.py", *args], cwd=START, capture_output=True, text=True, timeout=120
    )


class PublicTests(unittest.TestCase):
    def test_plan_runs(self):
        result = run("--plan")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scene", result.stdout)

    def test_render_frame_writes_ppm(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "f.ppm")
            result = run("--render-frame", "1.0", "--out", out)
            self.assertEqual(result.returncode, 0, result.stderr)
            with open(out, "rb") as handle:
                magic = handle.readline().strip()
                size = handle.readline().strip().split()
                maxval = handle.readline().strip()
        self.assertEqual(magic, b"P6")
        self.assertEqual(maxval, b"255")
        self.assertEqual(len(size), 2)
        self.assertGreater(int(size[0]), 0)


if __name__ == "__main__":
    unittest.main()
