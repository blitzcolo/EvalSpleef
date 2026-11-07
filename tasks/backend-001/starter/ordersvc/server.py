"""HTTP 路由与处理（骨架版）。

API 契约见 API.md；本骨架只实现订单创建/查询与 JSON 落盘的 happy path。
"""

import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .inventory import InventoryClient, InventoryUnavailable
from .store import Store, new_order_id

MAX_BODY = 64 * 1024


class App:
    def __init__(self, store: Store, inventory: InventoryClient) -> None:
        self.store = store
        self.inventory = inventory


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "ordersvc/0.1"

        def log_message(self, fmt, *args):  # 静默访问日志，验收只看行为
            return

        def _send(self, status: int, payload, headers=None) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, code: str, message: str, headers=None) -> None:
            self._send(status, {"error": {"code": code, "message": message}}, headers)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("body too large")
            return self.rfile.read(length) if length else b""

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send(200, {"status": "ok", "inventory": "ok"})
                return
            if self.path.startswith("/orders/"):
                order_id = self.path[len("/orders/"):]
                order = app.store.get_order(order_id)
                if order is None:
                    self._error(404, "not_found", "order not found")
                    return
                self._send(200, order)
                return
            self._error(404, "not_found", "unknown path")

        def do_POST(self) -> None:
            if self.path != "/orders":
                self._error(404, "not_found", "unknown path")
                return
            try:
                payload = json.loads(self._read_body().decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._error(400, "invalid_request", "malformed JSON")
                return
            if not isinstance(payload, dict):
                self._error(400, "invalid_request", "body must be an object")
                return
            sku = payload.get("sku")
            quantity = payload.get("quantity")
            if not isinstance(sku, str) or not sku.strip():
                self._error(400, "invalid_request", "sku is required")
                return
            if not isinstance(quantity, int) or isinstance(quantity, bool) or not 1 <= quantity <= 99:
                self._error(400, "invalid_request", "quantity must be an integer in 1..99")
                return
            try:
                stock = app.inventory.stock(sku)
            except InventoryUnavailable as exc:
                self._error(500, "internal_error", str(exc))
                return
            if stock is None or int(stock.get("available", 0)) < quantity:
                self._error(422, "out_of_stock", f"insufficient stock for {sku}")
                return
            order = {
                "order_id": new_order_id(),
                "sku": sku,
                "quantity": quantity,
                "total_cents": int(stock["price_cents"]) * quantity,
                "status": "created",
            }
            app.store.save_order(order)
            self._send(201, order)

    return Handler


def create_server(host: str, port: int, app: App) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(app))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="ordersvc")
    parser.add_argument("--port", type=int, default=int(os.environ.get("ORDERSVC_PORT", "0")))
    parser.add_argument("--state-dir", default=os.environ.get("ORDERSVC_STATE_DIR", "./state"))
    parser.add_argument(
        "--inventory-url",
        default=os.environ.get("ORDERSVC_INVENTORY_URL", "http://127.0.0.1:9000"),
    )
    parser.add_argument("--auth-tokens", default=os.environ.get("ORDERSVC_AUTH_TOKENS", ""))
    parser.add_argument("--rate-limit", default=os.environ.get("ORDERSVC_RATE_LIMIT", "5/10"))
    args = parser.parse_args(argv)
    del args.auth_tokens, args.rate_limit  # 骨架尚未实现鉴权与限流，见 API.md
    app = App(Store(args.state_dir), InventoryClient(args.inventory_url))
    httpd = create_server("127.0.0.1", args.port, app)
    print(f"ORDERSVC_LISTENING {httpd.server_address[1]}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0
