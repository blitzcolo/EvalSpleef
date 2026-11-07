"""End-to-end smoke test over real HTTP (also runnable as a script).

Starts the ``FakeOpenAIServer`` from ``evalspleef-testing`` and both gateway apps on
ephemeral ports in-process, then drives every required exchange through real HTTP:
key issuance, chat completions (stream and non-stream), usage via the admin API,
budget rejections (``cost_cap`` / ``token_cap``), a revoked key (401), a key used
against another endpoint (403) and an Anthropic Messages tool round trip in both
modes. Raw ``curl -i`` style transcripts are written to
``.omo/reports/gateway-core-evidence/``.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from evalspleef.gateway.admin_app import create_admin_app
from evalspleef.gateway.data_app import create_data_app
from evalspleef.gateway.state import GATEWAY_VERSION, GatewayState
from evalspleef.gateway.store import MemoryStore
from evalspleef.spi.gateway import COST_CAP, TOKEN_CAP, PriceSpec
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.secrets import DictSecretResolver

REPO_ROOT = Path(__file__).resolve().parents[3]
EVIDENCE_DIR = REPO_ROOT / ".omo" / "reports" / "gateway-core-evidence"

UPSTREAM_KEY = "upstream-secret-123"
ADMIN_TOKEN = "smoke-admin-token"
EXPIRES_AT = "2099-01-01T00:00:00Z"
TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the current weather for a city",
        "input_schema": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    }
]


class Transcripts:
    """Accumulates curl -i style transcripts and writes them numbered to a directory."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._items: list[tuple[str, str]] = []

    def record(self, slug: str, request_text: str, response_text: str) -> None:
        self._items.append((slug, f"{request_text}\n{response_text}"))

    def write(self) -> list[Path]:
        self._directory.mkdir(parents=True, exist_ok=True)
        for stale in self._directory.glob("*.http.txt"):
            stale.unlink()
        paths = []
        for index, (slug, text) in enumerate(self._items, start=1):
            path = self._directory / f"{index:02d}-{slug}.http.txt"
            path.write_text(text, encoding="utf-8")
            paths.append(path)
        return paths


def _format_request(method: str, path: str, headers: dict[str, str], body: Any) -> str:
    lines = [f"{method} {path} HTTP/1.1"]
    lines += [f"{key}: {value}" for key, value in headers.items()]
    text = "\n".join(lines) + "\n\n"
    if body is not None:
        text += body if isinstance(body, str) else json.dumps(body, indent=2)
    return text


def _format_response(status: int, reason: str, headers: dict[str, str], body: str) -> str:
    lines = [f"HTTP/1.1 {status} {reason}"]
    lines += [f"{key}: {value}" for key, value in headers.items()]
    return "\n".join(lines) + "\n\n" + body


_REASONS = {
    200: "OK",
    201: "Created",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    429: "Too Many Requests",
    502: "Bad Gateway",
}


class Exchange:
    """One client that records every request/response pair it makes."""

    def __init__(self, base_url: str, transcripts: Transcripts) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=30.0)
        self._transcripts = transcripts
        self.counter = 0

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        slug: str,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
        record: bool = True,
    ) -> httpx.Response:
        header_map = dict(headers or {})
        response = self._client.request(method, path, json=body, headers=header_map)
        if record:
            self.counter += 1
            shown = dict(header_map)
            if "Authorization" in shown:
                shown["Authorization"] = "Bearer <redacted>"
            if "x-api-key" in shown:
                shown["x-api-key"] = "<redacted>"
            request_text = _format_request(method, path, shown, body)
            response_headers = {
                key: value
                for key, value in response.headers.items()
                if key.lower() not in {"date", "content-length"}
            }
            response_text = _format_response(
                response.status_code,
                _REASONS.get(response.status_code, ""),
                response_headers,
                response.text,
            )
            self._transcripts.record(slug, request_text, response_text)
        return response

    def stream_text(
        self,
        slug: str,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        body: Any = None,
    ) -> tuple[int, dict[str, str], str]:
        header_map = dict(headers or {})
        with self._client.stream(method, path, json=body, headers=header_map) as response:
            text = response.read().decode("utf-8", errors="replace")
            status = response.status_code
            response_headers = dict(response.headers)
        self.counter += 1
        shown = dict(header_map)
        if "Authorization" in shown:
            shown["Authorization"] = "Bearer <redacted>"
        if "x-api-key" in shown:
            shown["x-api-key"] = "<redacted>"
        request_text = _format_request(method, path, shown, body)
        response_text = _format_response(
            status,
            _REASONS.get(status, ""),
            {
                k: v
                for k, v in response_headers.items()
                if k.lower() not in {"date", "content-length"}
            },
            text,
        )
        self._transcripts.record(slug, request_text, response_text)
        return status, response_headers, text


@contextmanager
def serve_app(app: Any) -> Iterator[str]:
    """Run a FastAPI app under uvicorn on an ephemeral port in a daemon thread."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    config = uvicorn.Config(app, log_level="error", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            listener.close()
            raise RuntimeError("gateway app failed to start")
        time.sleep(0.005)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def _expect(condition: bool, what: str) -> None:
    if not condition:
        raise AssertionError(f"e2e smoke failed: {what}")


def _sse_events(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        data = "\n".join(line[6:] for line in block.splitlines() if line.startswith("data: "))
        if data and data != "[DONE]":
            payload = json.loads(data)
            if isinstance(payload, dict):
                events.append(payload)
    return events


def run_smoke(evidence_dir: Path = EVIDENCE_DIR) -> list[Path]:
    transcripts = Transcripts(evidence_dir)
    store = MemoryStore()
    state = GatewayState(
        key_store=store,
        usage_store=store,
        secrets=DictSecretResolver({"vault://gw/upstream": UPSTREAM_KEY}),
    )
    with (
        FakeOpenAIServer(api_key=UPSTREAM_KEY, model="fake-model") as upstream,
        serve_app(create_data_app(state)) as data_url,
        serve_app(create_admin_app(state, ADMIN_TOKEN)) as admin_url,
    ):
        data = Exchange(data_url, transcripts)
        admin = Exchange(admin_url, transcripts)
        admin_headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
        try:
            _drive(data, admin, upstream, admin_headers)
        finally:
            data.close()
            admin.close()
    return transcripts.write()


def _drive(
    data: Exchange, admin: Exchange, upstream: FakeOpenAIServer, admin_headers: dict[str, str]
) -> None:
    # -- routes and prices ------------------------------------------------------------
    route_body = {
        "api_base": upstream.base_url,
        "model": "fake-model",
        "protocol": "chat_completions",
        "api_key_ref": "vault://gw/upstream",
        "default_params": {"max_tokens": 64},
        "provider_lock": {"response_field": "model", "pattern": "^fake-model$"},
        "cold_start_window_s": 0,
    }
    response = admin.request(
        "admin-put-route", "PUT", "/admin/routes/ep_demo1", headers=admin_headers, body=route_body
    )
    _expect(response.status_code == 200, f"PUT route -> {response.status_code}")
    price = PriceSpec(input_per_mtok=1.0, output_per_mtok=2.0, basis="api", table_version="smoke")
    response = admin.request(
        "admin-put-price",
        "PUT",
        "/admin/routes/ep_demo1/price",
        headers=admin_headers,
        body={
            "input_per_mtok": price.input_per_mtok,
            "output_per_mtok": price.output_per_mtok,
            "cached_per_mtok": price.cached_per_mtok,
            "basis": price.basis,
            "table_version": price.table_version,
        },
    )
    _expect(response.status_code == 200, f"PUT price -> {response.status_code}")
    second = dict(route_body)
    response = admin.request(
        "admin-put-route-2", "PUT", "/admin/routes/ep_demo2", headers=admin_headers, body=second
    )
    _expect(response.status_code == 200, f"PUT route 2 -> {response.status_code}")

    response = admin.request("admin-version", "GET", "/admin/version", headers=admin_headers)
    _expect(response.json()["version"] == GATEWAY_VERSION, "admin version mismatch")

    # -- key issuance ------------------------------------------------------------------
    response = admin.request(
        "admin-issue-key",
        "POST",
        "/admin/keys",
        headers=admin_headers,
        body={
            "trial_id": "trial-1",
            "endpoint_id": "demo1",
            "max_budget_usd": 1.0,
            "max_tokens": 1_000_000,
            "expires_at": EXPIRES_AT,
        },
    )
    _expect(response.status_code == 201, f"issue key -> {response.status_code}")
    key_main = response.json()["secret"]
    key_headers = {"Authorization": f"Bearer {key_main}"}

    # -- healthz and models -------------------------------------------------------------
    response = data.request("healthz", "GET", "/healthz")
    _expect(response.status_code == 200, "healthz")
    response = data.request("models", "GET", "/v1/models", headers=key_headers)
    _expect(response.status_code == 200, "models")
    _expect(
        [m["id"] for m in response.json()["data"]] == ["ep_demo1"],
        "models must list only the bound route",
    )

    # -- chat completions ----------------------------------------------------------------
    chat_body = {
        "model": "ep_demo1",
        "messages": [{"role": "user", "content": "say hi"}],
        "max_tokens": 500,
    }
    response = data.request(
        "chat-non-stream", "POST", "/v1/chat/completions", headers=key_headers, body=chat_body
    )
    _expect(response.status_code == 200, f"chat -> {response.status_code}")
    _expect(response.json()["choices"][0]["message"]["content"] == "fake reply", "chat content")
    _expect(response.headers.get("x-spleef-gateway-version") == GATEWAY_VERSION, "version header")
    forwarded = upstream.requests[-1].body
    _expect(forwarded["model"] == "fake-model", "upstream model name")
    _expect(forwarded["max_tokens"] == 64, "max_tokens ceiling applied")

    status, _, text = data.stream_text(
        "chat-stream",
        "POST",
        "/v1/chat/completions",
        headers=key_headers,
        body={**chat_body, "stream": True},
    )
    _expect(status == 200, f"chat stream -> {status}")
    _expect("data: [DONE]" in text, "stream must end with [DONE]")
    chunks = [e for e in _sse_events(text) if e.get("object") == "chat.completion.chunk"]
    _expect(any(chunk.get("usage") for chunk in chunks), "stream must carry a usage chunk")
    forwarded = upstream.requests[-1].body
    _expect(forwarded["stream_options"]["include_usage"] is True, "include_usage forwarded")

    # -- usage through the admin API ------------------------------------------------------
    response = admin.request(
        "admin-usage", "GET", "/admin/usage?trial_id=trial-1", headers=admin_headers
    )
    _expect(response.status_code == 200, "usage")
    records = response.json()
    _expect(len(records) == 2, f"expected 2 usage records, got {len(records)}")
    _expect(all(record["cost_usd"] > 0 for record in records), "usage cost must be positive")
    _expect(
        all(record["upstream_model"] == "fake-model" for record in records),
        "upstream model recorded",
    )

    # -- budget rejections ------------------------------------------------------------------
    response = admin.request(
        "admin-issue-key-cost",
        "POST",
        "/admin/keys",
        headers=admin_headers,
        body={
            "trial_id": "trial-cost",
            "endpoint_id": "demo1",
            "max_budget_usd": 0.000001,
            "max_tokens": 1_000_000,
            "expires_at": EXPIRES_AT,
        },
    )
    key_cost = response.json()["secret"]
    response = data.request(
        "chat-cost-cap",
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_cost}"},
        body=chat_body,
    )
    _expect(response.status_code == 429, f"cost cap -> {response.status_code}")
    _expect(response.json()["error"]["code"] == COST_CAP, "error code must be cost_cap")

    response = admin.request(
        "admin-issue-key-tokens",
        "POST",
        "/admin/keys",
        headers=admin_headers,
        body={
            "trial_id": "trial-tokens",
            "endpoint_id": "demo1",
            "max_budget_usd": 1.0,
            "max_tokens": 10,
            "expires_at": EXPIRES_AT,
        },
    )
    key_tokens = response.json()["secret"]
    response = data.request(
        "chat-token-cap",
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_tokens}"},
        body={**chat_body, "max_tokens": 500},
    )
    _expect(response.status_code == 429, f"token cap -> {response.status_code}")
    _expect(response.json()["error"]["code"] == TOKEN_CAP, "error code must be token_cap")

    # -- revoked key -------------------------------------------------------------------------
    response = admin.request(
        "admin-issue-key-revoke",
        "POST",
        "/admin/keys",
        headers=admin_headers,
        body={
            "trial_id": "trial-revoked",
            "endpoint_id": "demo1",
            "max_budget_usd": 1.0,
            "max_tokens": 1000,
            "expires_at": EXPIRES_AT,
        },
    )
    key_revoked_id = response.json()["key_id"]
    key_revoked = response.json()["secret"]
    response = admin.request(
        "admin-revoke-key", "DELETE", f"/admin/keys/{key_revoked_id}", headers=admin_headers
    )
    _expect(response.status_code == 204, "revoke")
    response = data.request(
        "chat-revoked-key",
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_revoked}"},
        body=chat_body,
    )
    _expect(response.status_code == 401, f"revoked key -> {response.status_code}")

    # -- cross-endpoint isolation -------------------------------------------------------------
    response = admin.request(
        "admin-issue-key-demo2",
        "POST",
        "/admin/keys",
        headers=admin_headers,
        body={
            "trial_id": "trial-2",
            "endpoint_id": "demo2",
            "max_budget_usd": 1.0,
            "max_tokens": 1000,
            "expires_at": EXPIRES_AT,
        },
    )
    key_demo2 = response.json()["secret"]
    response = data.request(
        "chat-cross-endpoint",
        "POST",
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key_demo2}"},
        body=chat_body,
    )
    _expect(response.status_code == 403, f"cross endpoint -> {response.status_code}")

    # -- anthropic messages: tool round trip, non-stream ----------------------------------------
    anthropic_round_1 = {
        "model": "ep_demo1",
        "max_tokens": 100,
        "tools": TOOLS,
        "tool_choice": {"type": "any"},
        "messages": [{"role": "user", "content": "What is the weather in Berlin?"}],
    }
    response = data.request(
        "anthropic-tool-call", "POST", "/v1/messages", headers=key_headers, body=anthropic_round_1
    )
    _expect(response.status_code == 200, f"anthropic -> {response.status_code}")
    message = response.json()
    tool_blocks = [b for b in message["content"] if b["type"] == "tool_use"]
    _expect(len(tool_blocks) == 1, "expected one tool_use block")
    _expect(tool_blocks[0]["name"] == "get_weather", "tool name")
    _expect(tool_blocks[0]["input"].get("city") == "x", "tool input filled from schema")
    _expect(message["stop_reason"] == "tool_use", "stop_reason tool_use")

    anthropic_round_2 = {
        **anthropic_round_1,
        "messages": [
            anthropic_round_1["messages"][0],
            {"role": "assistant", "content": tool_blocks},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_blocks[0]["id"], "content": "sunny"}
                ],
            },
        ],
    }
    response = data.request(
        "anthropic-tool-result", "POST", "/v1/messages", headers=key_headers, body=anthropic_round_2
    )
    _expect(response.status_code == 200, "anthropic round 2")
    text = "".join(b["text"] for b in response.json()["content"] if b["type"] == "text")
    _expect("tool-result:1" in text, f"expected tool result answer, got {text!r}")

    # -- anthropic messages: tool round trip, stream (x-api-key auth) ----------------------------
    status, _, text = data.stream_text(
        "anthropic-stream-tool-call",
        "POST",
        "/v1/messages",
        headers={"x-api-key": key_main},
        body={**anthropic_round_1, "stream": True},
    )
    _expect(status == 200, f"anthropic stream -> {status}")
    events = _sse_events(text)
    starts = [
        e
        for e in events
        if e.get("type") == "content_block_start" and e["content_block"]["type"] == "tool_use"
    ]
    _expect(
        starts and starts[0]["content_block"]["name"] == "get_weather", "streamed tool_use block"
    )

    anthropic_stream_round_2 = {
        **anthropic_round_1,
        "stream": True,
        "messages": [
            anthropic_round_1["messages"][0],
            {"role": "assistant", "content": tool_blocks},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": tool_blocks[0]["id"], "content": "sunny"}
                ],
            },
        ],
    }
    status, _, text = data.stream_text(
        "anthropic-stream-tool-result",
        "POST",
        "/v1/messages",
        headers={"x-api-key": key_main},
        body=anthropic_stream_round_2,
    )
    _expect(status == 200, "anthropic stream round 2")
    deltas = [e for e in _sse_events(text) if e.get("type") == "content_block_delta"]
    text_out = "".join(e["delta"]["text"] for e in deltas)
    _expect("tool-result:1" in text_out, f"streamed tool answer, got {text_out!r}")

    # -- events -----------------------------------------------------------------------------------
    response = admin.request("admin-events", "GET", "/admin/events?after=0", headers=admin_headers)
    _expect(response.status_code == 200, "events")
    event_types = {event["type"] for event in response.json()["events"]}
    for expected in ("key.issued", "request.completed", "budget.rejected"):
        _expect(expected in event_types, f"missing event {expected}")

    print(f"e2e smoke OK: {data.counter + admin.counter} exchanges recorded")


def main() -> int:
    try:
        paths = run_smoke()
    except Exception as exc:
        import traceback

        traceback.print_exc()
        print(f"e2e smoke FAILED: {exc}", file=sys.stderr)
        return 1
    for path in paths:
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
