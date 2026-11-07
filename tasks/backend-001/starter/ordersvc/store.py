"""订单与幂等记录的持久化（JSON 文件，标准库）。"""

import json
import os
import secrets
import threading


def new_order_id() -> str:
    return "ord-" + secrets.token_hex(6)


class Store:
    """极简持久化：orders.json 保存订单，idempotency.json 保存幂等记录。"""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir
        os.makedirs(state_dir, exist_ok=True)
        self._lock = threading.Lock()
        self._orders = self._load("orders.json", {})
        self._idempotency = self._load("idempotency.json", {})

    def _load(self, name: str, default):
        path = os.path.join(self.state_dir, name)
        if not os.path.exists(path):
            return default
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _flush(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        for name, data in (("orders.json", self._orders), ("idempotency.json", self._idempotency)):
            tmp = os.path.join(self.state_dir, name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            os.replace(tmp, os.path.join(self.state_dir, name))

    def save_order(self, order: dict) -> None:
        with self._lock:
            self._orders[order["order_id"]] = order
            self._flush()

    def get_order(self, order_id: str):
        with self._lock:
            return self._orders.get(order_id)

    def get_idempotent(self, key: str):
        with self._lock:
            return self._idempotency.get(key)

    def save_idempotent(self, key: str, record: dict) -> None:
        with self._lock:
            self._idempotency[key] = record
            self._flush()
