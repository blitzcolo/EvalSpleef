"""库存依赖客户端（urllib，标准库）。"""

import json
import urllib.error
import urllib.request


class InventoryUnavailable(Exception):
    """库存服务不可用（连接失败/超时/5xx）。"""


class InventoryClient:
    def __init__(self, base_url: str, timeout: float = 1.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def stock(self, sku: str) -> dict | None:
        """返回 {"sku","price_cents","available"}；SKU 不存在返回 None。"""
        url = f"{self.base_url}/stock/{sku}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise InventoryUnavailable(f"inventory HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise InventoryUnavailable(str(exc)) from exc

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=self.timeout) as response:
                return response.status == 200
        except Exception:
            return False
