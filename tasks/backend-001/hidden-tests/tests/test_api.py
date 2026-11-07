"""backend-001 隐藏 API 测试：按 API.md 契约对候选服务发真实 HTTP 请求。

运行方式（由 hidden-tests/run.sh 调用）：
    BACKEND_CANDIDATE=<候选工程根目录> python3 -m unittest discover -s hidden-tests/tests
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from inventory_stub import StubInventory  # noqa: E402

CANDIDATE = os.environ.get("BACKEND_CANDIDATE") or os.path.abspath(
    os.path.join(HERE, "..", "..", "starter")
)
TOKENS = "tok-a,tok-b,tok-c"


class ServiceProcess:
    def __init__(self, state_dir: str, inventory_url: str) -> None:
        self.state_dir = state_dir
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "ordersvc",
                "--port", "0",
                "--state-dir", state_dir,
                "--inventory-url", inventory_url,
                "--auth-tokens", TOKENS,
                "--rate-limit", "5/10",
            ],
            cwd=CANDIDATE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.port = None
        line = self._read_start_line(timeout=20)
        if not line.startswith("ORDERSVC_LISTENING "):
            self.close()
            raise AssertionError(f"启动行不符合契约: {line!r}")
        self.port = int(line.split()[1])

    def _read_start_line(self, timeout: float) -> str:
        lines = queue.Queue()

        def pump():
            for line in self.proc.stdout:
                lines.put(line.rstrip("\n"))
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise AssertionError(f"服务提前退出: {self.proc.stderr.read()[-2000:]}")
                continue
            if line is None:
                raise AssertionError(f"服务未输出监听行: {self.proc.stderr.read()[-2000:]}")
            return line
        raise AssertionError("等待服务启动超时")

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)


def request(base: str, method: str, path: str, body=None, token="tok-a", key=None,
            content_type="application/json", timeout: float = 15.0):
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method=method)
    if token is not None:
        req.add_header("Authorization", "Bearer " + token)
    if key is not None:
        req.add_header("Idempotency-Key", key)
    if content_type is not None:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            return response.status, dict(response.headers), json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return exc.code, dict(exc.headers), payload


class BackendTestCase(unittest.TestCase):
    maxDiff = 4000

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="backend001_")
        self.stub = StubInventory().start()
        self.svc = ServiceProcess(os.path.join(self.tmp, "state"), self.stub.url)

    def tearDown(self) -> None:
        self.svc.close()
        self.stub.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers -----------------------------------------------------------
    def assert_error(self, result, status: int, code: str) -> dict:
        got_status, headers, payload = result
        self.assertEqual(got_status, status, payload)
        self.assertEqual(payload["error"]["code"], code)
        return headers

    def create(self, token="tok-a", key="key-1", body=None):
        return request(self.svc.base, "POST", "/orders",
                       body={"sku": "A-1", "quantity": 2} if body is None else body,
                       token=token, key=key)


class TestHealth(BackendTestCase):
    def test_health_ok_and_no_auth(self):
        status, _, payload = request(self.svc.base, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"status": "ok", "inventory": "ok"})


class TestAuth(BackendTestCase):
    def test_missing_token_is_401(self):
        result = request(self.svc.base, "POST", "/orders",
                         body={"sku": "A-1", "quantity": 1}, token=None, key="k1")
        self.assert_error(result, 401, "unauthorized")

    def test_unknown_token_is_401(self):
        self.assert_error(self.create(token="tok-nope"), 401, "unauthorized")

    def test_get_requires_auth(self):
        _, _, order = self.create()
        status, _, payload = request(self.svc.base, "GET", "/orders/" + order["order_id"], token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")


class TestCreateOrder(BackendTestCase):
    def test_create_order_success(self):
        status, _, order = self.create()
        self.assertEqual(status, 201)
        self.assertRegex(order["order_id"], r"^ord-[0-9a-f]{12}$")
        self.assertEqual(order["sku"], "A-1")
        self.assertEqual(order["quantity"], 2)
        self.assertEqual(order["total_cents"], 1000)
        self.assertEqual(order["status"], "created")

    def test_invalid_body_is_400(self):
        for body in ({"sku": "", "quantity": 1}, {"sku": "A-1", "quantity": 0},
                     {"sku": "A-1", "quantity": 100}, {"sku": "A-1"}, []):
            result = self.create(key="bad-" + json.dumps(body, sort_keys=True), body=body)
            self.assert_error(result, 400, "invalid_request")

    def test_malformed_json_is_400(self):
        result = self.create(body=b"{not json")
        self.assert_error(result, 400, "invalid_request")

    def test_missing_idempotency_key_is_400(self):
        result = request(self.svc.base, "POST", "/orders",
                         body={"sku": "A-1", "quantity": 1}, token="tok-b", key=None)
        self.assert_error(result, 400, "invalid_request")

    def test_out_of_stock_is_422(self):
        result = self.create(token="tok-b", key="oos-1", body={"sku": "B-1", "quantity": 1})
        self.assert_error(result, 422, "out_of_stock")

    def test_unknown_sku_is_422(self):
        result = self.create(token="tok-c", key="unk-1", body={"sku": "Z-9", "quantity": 1})
        self.assert_error(result, 422, "out_of_stock")


class TestIdempotency(BackendTestCase):
    def test_replay_same_body_returns_same_order(self):
        first = self.create(key="idem-1")
        second = self.create(key="idem-1")
        self.assertEqual(first[0], 201)
        self.assertEqual(second[0], 201)
        self.assertEqual(first[2], second[2])
        self.assertEqual(second[1].get("Idempotent-Replayed"), "true")

    def test_same_key_different_body_conflicts(self):
        self.create(key="idem-2", body={"sku": "A-1", "quantity": 1})
        result = self.create(key="idem-2", body={"sku": "A-1", "quantity": 2})
        self.assert_error(result, 409, "idempotency_conflict")

    def test_replay_does_not_create_second_order(self):
        order = self.create(key="idem-3")[2]
        replay = self.create(key="idem-3")[2]
        self.assertEqual(order["order_id"], replay["order_id"])


class TestReadOrder(BackendTestCase):
    def test_get_order(self):
        _, _, order = self.create()
        status, _, fetched = request(self.svc.base, "GET", "/orders/" + order["order_id"])
        self.assertEqual(status, 200)
        self.assertEqual(fetched, order)

    def test_unknown_order_404(self):
        status, _, payload = request(self.svc.base, "GET", "/orders/ord-000000000000")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")


class TestRateLimit(BackendTestCase):
    def test_burst_over_limit_gets_429_with_retry_after(self):
        codes = []
        for index in range(6):
            status, headers, payload = self.create(token="tok-c", key=f"rate-{index}")
            codes.append(status)
            if status == 429:
                self.assertEqual(payload["error"]["code"], "rate_limited")
                self.assertEqual(headers.get("Retry-After"), "10")
        self.assertEqual(codes[-1], 429, codes)
        self.assertEqual(codes[:5], [201] * 5)

    def test_other_token_not_limited(self):
        for index in range(5):
            self.create(token="tok-a", key=f"iso-a-{index}")
        status, _, _ = self.create(token="tok-b", key="iso-b-1")
        self.assertEqual(status, 201)


class TestPersistence(BackendTestCase):
    def test_order_and_idempotency_survive_restart(self):
        _, _, order = self.create(key="persist-1")
        self.svc.close()
        self.svc = ServiceProcess(os.path.join(self.tmp, "state"), self.stub.url)
        status, _, fetched = request(self.svc.base, "GET", "/orders/" + order["order_id"])
        self.assertEqual(status, 200)
        self.assertEqual(fetched, order)
        status, headers, replayed = request(
            self.svc.base, "POST", "/orders",
            body={"sku": "A-1", "quantity": 2}, token="tok-b", key="persist-1",
        )
        self.assertEqual(status, 201)
        self.assertEqual(replayed["order_id"], order["order_id"])
        self.assertEqual(headers.get("Idempotent-Replayed"), "true")


class TestFaultInjection(BackendTestCase):
    def test_inventory_down_degrades_and_recovers(self):
        self.stub.stop()
        result = self.create(token="tok-a", key="fault-1")
        headers = self.assert_error(result, 503, "inventory_unavailable")
        self.assertEqual(headers.get("Retry-After"), "5")
        status, _, health = request(self.svc.base, "GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(health, {"status": "degraded", "inventory": "unavailable"})

        self.stub = StubInventory().start()
        self.svc.close()
        self.svc = ServiceProcess(os.path.join(self.tmp, "state"), self.stub.url)
        status, _, health = request(self.svc.base, "GET", "/health", token=None)
        self.assertEqual(health["status"], "ok")
        status, _, order = self.create(token="tok-b", key="fault-1")
        self.assertEqual(status, 201, order)


if __name__ == "__main__":
    unittest.main()
