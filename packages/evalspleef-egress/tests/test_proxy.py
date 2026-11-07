from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from evalspleef.egress.policy import MirrorRoute, Policy
from evalspleef.egress.proxy import EgressProxy
from evalspleef.spi import NetMode

URL_FIELDS = {
    "ts",
    "trial_id",
    "round",
    "process",
    "method",
    "host",
    "path",
    "status",
    "bytes",
    "blocked_reason",
}


@dataclass(frozen=True)
class ServedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeHttpServer:
    """A local HTTP/1.1 server the proxy can forward to; closes after one response."""

    def __init__(self) -> None:
        self.requests: list[ServedRequest] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        sockets = self._server.sockets or ()
        self.port = int(sockets[0].getsockname()[1])

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            writer.close()
            return
        lines = head.decode("iso-8859-1").split("\r\n")
        method, path, _ = lines[0].split(" ")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = await reader.readexactly(length) if length else b""
        self.requests.append(ServedRequest(method, path, headers, body))
        payload = f"{method} {path}".encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + payload
        )
        await writer.drain()
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None


@pytest.fixture
async def http_server() -> AsyncIterator[FakeHttpServer]:
    server = FakeHttpServer()
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def proxy_request(address: tuple[str, int], raw: bytes) -> tuple[int, dict[str, str], bytes]:
    reader, writer = await asyncio.open_connection(*address)
    try:
        writer.write(raw)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split(" ")[1])
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if line:
                name, _, value = line.partition(":")
                headers[name.strip().lower()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = await asyncio.wait_for(reader.readexactly(length), 5) if length else b""
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError):
            await writer.wait_closed()
    return status, headers, body


def read_records(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def http_request(method: str, host: str, port: int, path: str, *, body: bytes = b"") -> bytes:
    lines = [
        f"{method} http://{host}:{port}{path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Connection: close",
    ]
    if body:
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


async def test_allowed_host_is_proxied_and_logged(
    tmp_path: Path, http_server: FakeHttpServer
) -> None:
    policy = Policy(mode=NetMode.FULL)
    async with EgressProxy(policy, port=0, log_dir=tmp_path, trial_id="trial-a", round=2) as proxy:
        status, _, body = await proxy_request(
            proxy.address,
            http_request("GET", "127.0.0.1", http_server.port, "/hello?x=1"),
        )
        assert status == 200
        assert body == b"GET /hello?x=1"
        assert http_server.requests[0].path == "/hello?x=1"

    records = read_records(tmp_path / "url.jsonl")
    assert len(records) == 1
    record = records[0]
    assert set(record) == URL_FIELDS
    assert record["trial_id"] == "trial-a"
    assert record["round"] == 2
    assert record["method"] == "GET"
    assert record["host"] == "127.0.0.1"
    assert record["path"] == "/hello?x=1"
    assert record["status"] == 200
    assert record["bytes"] == len(body)
    assert record["blocked_reason"] is None

    dns = read_records(tmp_path / "dns.jsonl")
    assert len(dns) == 1
    assert dns[0]["qname"] == "127.0.0.1"
    assert dns[0]["blocked"] is False
    assert dns[0]["answer"] == ["127.0.0.1"]


async def test_wildcard_allowlist_matches_through_proxy(
    tmp_path: Path, http_server: FakeHttpServer
) -> None:
    # The test has no DNS control, so the loopback literal stands in for a subdomain:
    # "127.0.0.1" ends with ".0.0.1" exactly as "api.example.com" ends with ".example.com".
    policy = Policy(mode=NetMode.DOCS_WHITELIST, allow=("*.0.0.1",))
    async with EgressProxy(policy, port=0, log_dir=tmp_path, trial_id="trial-w") as proxy:
        status, _, body = await proxy_request(
            proxy.address, http_request("GET", "127.0.0.1", http_server.port, "/wild")
        )
    assert status == 200
    assert body == b"GET /wild"
    assert read_records(tmp_path / "url.jsonl")[0]["status"] == 200


async def test_denied_host_gets_403_with_reason(
    tmp_path: Path, http_server: FakeHttpServer
) -> None:
    policy = Policy(mode=NetMode.DOCS_WHITELIST, allow=("allowed.example",))
    async with EgressProxy(policy, port=0, log_dir=tmp_path, trial_id="trial-b") as proxy:
        status, _, body = await proxy_request(
            proxy.address,
            http_request("GET", "denied.example", 80, "/secret"),
        )
    assert status == 403
    assert json.loads(body) == {"blocked_reason": "host_not_allowed"}
    assert http_server.requests == []

    record = read_records(tmp_path / "url.jsonl")[0]
    assert record["status"] == 403
    assert record["host"] == "denied.example"
    assert record["path"] == "/secret"
    assert record["blocked_reason"] == "host_not_allowed"
    dns = read_records(tmp_path / "dns.jsonl")
    assert dns[0]["qname"] == "denied.example"
    assert dns[0]["blocked"] is True


async def test_post_through_package_mirror_is_403(
    tmp_path: Path, http_server: FakeHttpServer
) -> None:
    mirror = MirrorRoute("pip", ("pypi.org",), f"http://127.0.0.1:{http_server.port}/mirror")
    policy = Policy(mode=NetMode.PACKAGE_MIRROR, mirrors=(mirror,))
    async with EgressProxy(policy, port=0, log_dir=tmp_path, trial_id="trial-c") as proxy:
        status, _, body = await proxy_request(
            proxy.address,
            http_request("POST", "pypi.org", 80, "/simple/pkg/", body=b"x=1"),
        )
    assert status == 403
    assert json.loads(body) == {"blocked_reason": "method_not_allowed"}
    assert http_server.requests == []
    record = read_records(tmp_path / "url.jsonl")[0]
    assert record["method"] == "POST"
    assert record["blocked_reason"] == "method_not_allowed"


async def test_package_mirror_rewrites_host_and_path(
    tmp_path: Path, http_server: FakeHttpServer
) -> None:
    mirror = MirrorRoute(
        "pip", ("pypi.org", "files.pythonhosted.org"), f"http://127.0.0.1:{http_server.port}/pypi"
    )
    policy = Policy(mode=NetMode.PACKAGE_MIRROR, mirrors=(mirror,))
    async with EgressProxy(policy, port=0, log_dir=tmp_path, trial_id="trial-d") as proxy:
        status, _, body = await proxy_request(
            proxy.address,
            http_request("GET", "pypi.org", 80, "/simple/requests/"),
        )
    assert status == 200
    assert body == b"GET /pypi/simple/requests/"
    assert http_server.requests[0].headers["host"] == f"127.0.0.1:{http_server.port}"

    record = read_records(tmp_path / "url.jsonl")[0]
    assert record["host"] == "pypi.org"
    assert record["path"] == "/simple/requests/"
    assert record["status"] == 200


async def test_metadata_address_is_blocked(tmp_path: Path, http_server: FakeHttpServer) -> None:
    policy = Policy(mode=NetMode.FULL)
    async with EgressProxy(policy, port=0, log_dir=tmp_path, trial_id="trial-e") as proxy:
        status, _, body = await proxy_request(
            proxy.address,
            http_request("GET", "169.254.169.254", 80, "/latest/meta-data/"),
        )
    assert status == 403
    assert json.loads(body) == {"blocked_reason": "blocked_cidr"}
    record = read_records(tmp_path / "url.jsonl")[0]
    assert record["host"] == "169.254.169.254"
    assert record["blocked_reason"] == "blocked_cidr"


async def test_connect_tunnel_is_allowed_and_logged(
    tmp_path: Path, http_server: FakeHttpServer
) -> None:
    policy = Policy(mode=NetMode.FULL)
    async with EgressProxy(policy, port=0, log_dir=tmp_path, trial_id="trial-f") as proxy:
        reader, writer = await asyncio.open_connection(*proxy.address)
        try:
            writer.write(
                (
                    f"CONNECT 127.0.0.1:{http_server.port} HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{http_server.port}\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            assert head.startswith(b"HTTP/1.1 200")
            writer.write(
                (
                    f"GET /inside HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{http_server.port}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode()
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()

    assert b"GET /inside" in response
    records = read_records(tmp_path / "url.jsonl")
    assert len(records) == 1
    assert records[0]["method"] == "CONNECT"
    assert records[0]["host"] == "127.0.0.1"
    assert records[0]["status"] == 200
    assert records[0]["blocked_reason"] is None


async def test_connect_to_denied_host_is_403(tmp_path: Path, http_server: FakeHttpServer) -> None:
    policy = Policy(mode=NetMode.DOCS_WHITELIST, allow=("allowed.example",))
    async with EgressProxy(policy, port=0, log_dir=tmp_path, trial_id="trial-g") as proxy:
        status, _, body = await proxy_request(
            proxy.address,
            b"CONNECT denied.example:443 HTTP/1.1\r\nHost: denied.example:443\r\n\r\n",
        )
    assert status == 403
    assert json.loads(body)["blocked_reason"] == "host_not_allowed"
