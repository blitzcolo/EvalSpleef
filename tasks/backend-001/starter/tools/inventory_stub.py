#!/usr/bin/env python3
"""本地库存桩（仅开发/联调用，不属于验收逻辑）。"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STOCK = {
    "A-1": {"price_cents": 500, "available": 10},
    "B-1": {"price_cents": 300, "available": 0},
}


def make_handler():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _send(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok"})
                return
            if self.path.startswith("/stock/"):
                sku = self.path[len("/stock/"):]
                info = STOCK.get(sku)
                if info is None:
                    self._send(404, {"error": "unknown sku"})
                    return
                self._send(200, {"sku": sku, **info})
                return
            self._send(404, {"error": "not found"})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler())
    print(f"INVENTORY_LISTENING {httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
