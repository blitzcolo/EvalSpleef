"""验收侧库存桩：可启停，用于故障注入测试（不属于候选工程）。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_STOCK = {
    "A-1": {"price_cents": 500, "available": 10},
    "B-1": {"price_cents": 300, "available": 0},
}


class StubInventory:
    def __init__(self, stock=None) -> None:
        self.stock = dict(DEFAULT_STOCK if stock is None else stock)
        self.httpd = None
        self.thread = None
        self.port = 0

    def start(self) -> "StubInventory":
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                return

            def _send(self, status: int, payload) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path == "/health":
                    self._send(200, {"status": "ok"})
                    return
                if self.path.startswith("/stock/"):
                    sku = self.path[len("/stock/"):]
                    info = stub.stock.get(sku)
                    if info is None:
                        self._send(404, {"error": "unknown sku"})
                        return
                    self._send(200, {"sku": sku, **info})
                    return
                self._send(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
            self.thread = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"
