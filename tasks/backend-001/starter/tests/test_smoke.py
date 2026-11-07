"""现有冒烟测试（starter 即可通过；修复后必须继续通过）。"""

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

START = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, START)

from tools.inventory_stub import make_handler  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


class StubServer:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler())
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class ServiceProcess:
    def __init__(self, state_dir, inventory_url):
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "ordersvc", "--port", "0",
                "--state-dir", state_dir, "--inventory-url", inventory_url,
                "--auth-tokens", "tok-a,tok-b", "--rate-limit", "5/10",
            ],
            cwd=START,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.port = None
        lines = queue.Queue()
        threading.Thread(target=self._pump, args=(lines,), daemon=True).start()
        try:
            line = lines.get(timeout=20)
        except queue.Empty:
            self.proc.kill()
            raise AssertionError("ordersvc 启动超时")
        if not line.startswith("ORDERSVC_LISTENING "):
            self.proc.kill()
            raise AssertionError(f"启动行不符合契约: {line!r}")
        self.port = int(line.split()[1])

    def _pump(self, lines):
        for line in self.proc.stdout:
            lines.put(line.rstrip("\n"))
        lines.put("")

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.stub = StubServer().__enter__()
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = ServiceProcess(self.tmp.name, self.stub.url)

    def tearDown(self):
        self.svc.close()
        self.stub.__exit__(None, None, None)
        self.tmp.cleanup()

    def request(self, method, path, body=None, token="tok-a", key="smoke-key-1"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.svc.base + path, data=data, method=method)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        if key:
            req.add_header("Idempotency-Key", key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def test_health(self):
        status, payload = self.request("GET", "/health", token=None, key=None)
        self.assertEqual(status, 200)
        self.assertIn("status", payload)

    def test_create_and_get_order(self):
        status, order = self.request("POST", "/orders", {"sku": "A-1", "quantity": 2}, key="smoke-1")
        self.assertEqual(status, 201, order)
        self.assertTrue(order["order_id"].startswith("ord-"))
        self.assertEqual(order["total_cents"], 1000)
        status, fetched = self.request("GET", "/orders/" + order["order_id"], key="smoke-2")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["order_id"], order["order_id"])

    def test_unknown_order(self):
        status, payload = self.request("GET", "/orders/ord-000000000000", key="smoke-3")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
