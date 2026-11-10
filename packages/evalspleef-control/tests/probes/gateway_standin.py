"""In-process gateway stand-in used by the probe and endpoint tests.

A tiny pass-through app served by uvicorn on an ephemeral port. It authenticates the bearer
secret with ``InMemoryGatewayAdmin``, resolves the public model name through the routes the
control plane registered, and forwards over real HTTP to the upstream (a ``FakeOpenAIServer``).
``/v1/messages`` and ``/v1/responses`` reuse the real gateway's protocol converters so P-13 runs
the same conversion code as production. A 503 from the upstream is retried immediately while
the route declares a cold-start window, and the response then carries the cold-start header.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Mapping
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from evalspleef.gateway.protocols import (
    anthropic_request_to_chat,
    chat_response_to_anthropic,
    chat_response_to_responses,
    responses_request_to_chat,
)
from evalspleef.spi.gateway import COLD_START_HEADER
from evalspleef.spi.model import UpstreamRoute
from evalspleef.testing.fake_gateway import InMemoryGatewayAdmin, TrialKeyRejected

MAX_COLD_START_ATTEMPTS = 20


class GatewayStandIn:
    """Sync context manager serving the stand-in; ``url`` is the data-plane root."""

    def __init__(
        self, admin: InMemoryGatewayAdmin, upstream_map: Mapping[str, str] | None = None
    ) -> None:
        self.admin = admin
        self.upstream_map = dict(upstream_map or {})
        """Maps a registered ``api_base`` to the local URL that actually serves it."""
        self.requests: list[tuple[str, str]] = []
        """(path, model name) of every request the stand-in handled."""
        self._app = FastAPI()
        for path in ("/v1/chat/completions", "/v1/messages", "/v1/responses"):
            self._app.add_api_route(path, self._handle, methods=["POST"])
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self.url = ""

    def __enter__(self) -> GatewayStandIn:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(self._app, log_level="error", access_log=False))
        thread = threading.Thread(
            target=server.run, kwargs={"sockets": [listener]}, name="gateway-standin", daemon=True
        )
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            if not thread.is_alive() or time.monotonic() > deadline:
                listener.close()
                raise RuntimeError("gateway stand-in failed to start")
            time.sleep(0.005)
        self._socket, self._server, self._thread = listener, server, thread
        self.url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self._socket is not None:
            self._socket.close()

    # -- request handling -------------------------------------------------------------

    @staticmethod
    def _error(status: int, code: str, message: str) -> JSONResponse:
        return JSONResponse({"error": {"type": code, "code": code, "message": message}}, status)

    def _route_for(self, name: str) -> UpstreamRoute | None:
        return self.admin.routes.get(name)

    async def _handle(self, request: Request) -> Response:
        authorization = request.headers.get("authorization", "")
        try:
            self.admin.authenticate(authorization.removeprefix("Bearer "))
        except TrialKeyRejected as exc:
            return self._error(401, "invalid_key", str(exc))
        raw: dict[str, Any] = await request.json()
        path = request.url.path
        route = self._route_for(str(raw.get("model", "")))
        self.requests.append((path, str(raw.get("model", ""))))
        if route is None:
            return self._error(404, "model_not_found", f"no route for {raw.get('model')!r}")

        flavor = {
            "/v1/messages": "anthropic_messages",
            "/v1/responses": "responses",
        }.get(path, "chat")
        body = (
            anthropic_request_to_chat(raw)
            if flavor == "anthropic_messages"
            else responses_request_to_chat(raw)
            if flavor == "responses"
            else dict(raw)
        )
        body["model"] = route.model
        base = self.upstream_map.get(route.api_base, route.api_base)

        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0)) as client:
            if body.get("stream"):
                return await self._stream(client, base, body)
            started = time.monotonic()
            attempts = 0
            while True:
                upstream = await client.post(f"{base}/chat/completions", json=body)
                attempts += 1
                retry = (
                    upstream.status_code == 503
                    and route.cold_start_window_s > 0
                    and attempts < MAX_COLD_START_ATTEMPTS
                )
                if not retry:
                    break
            headers: dict[str, str] = {}
            if attempts > 1 and upstream.status_code == 200:
                headers[COLD_START_HEADER] = str(max(1, int((time.monotonic() - started) * 1000)))
            payload: dict[str, Any] = upstream.json()
            if upstream.status_code != 200:
                return JSONResponse(payload, status_code=upstream.status_code)
            if flavor == "anthropic_messages":
                payload = chat_response_to_anthropic(payload, model=str(raw.get("model")))
            elif flavor == "responses":
                payload = chat_response_to_responses(payload, model=str(raw.get("model")))
            return JSONResponse(payload, headers=headers)

    async def _stream(self, client: httpx.AsyncClient, base: str, body: dict[str, Any]) -> Response:
        # A dedicated client keeps the upstream stream open for the lifetime of the response.
        upstream_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        request = upstream_client.build_request("POST", f"{base}/chat/completions", json=body)
        upstream = await upstream_client.send(request, stream=True)
        if upstream.status_code != 200:
            data = json.loads(await upstream.aread())
            await upstream.aclose()
            await upstream_client.aclose()
            return JSONResponse(data, status_code=upstream.status_code)

        async def relay() -> Any:
            try:
                async for line in upstream.aiter_lines():
                    yield f"{line}\n"
            finally:
                await upstream.aclose()
                await upstream_client.aclose()

        return StreamingResponse(relay(), media_type="text/event-stream")
