"""Asyncio HTTP forward proxy enforcing a :class:`~evalspleef.egress.policy.Policy`.

Supports both plain HTTP (absolute-form requests) and HTTPS via ``CONNECT`` tunnels. Every
decision uses the CONNECT authority or the request ``Host`` against the policy; denied
requests receive ``403`` with a JSON body carrying ``blocked_reason`` and a URL log line.

Each client connection is handled independently. Upstream connects are bounded by a timeout;
tunnel reads are not, because an idle CONNECT tunnel is legal. Logs are written by this
process into ``log_dir`` so they never live inside the sandbox (PRD D.3 / I16).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import socket
import ssl
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from .logs import AuditLog
from .policy import BLOCK_BLOCKED_CIDR, Policy, Route

_HEAD_LIMIT = 65536
_CHUNK = 65536
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "proxy-authenticate",
        "te",
        "trailer",
        "upgrade",
    }
)
_METHOD_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_REASON_PHRASES = {
    400: "Bad Request",
    403: "Forbidden",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    501: "Not Implemented",
    502: "Bad Gateway",
}


def _reason_phrase(status: int) -> str:
    return _REASON_PHRASES.get(status, "Error")


def _error_body(status: int, reason: str) -> bytes:
    key = "blocked_reason" if status == 403 else "error"
    return (json.dumps({key: reason}, separators=(",", ":")) + "\n").encode()


def _qtype(qname: str, addresses: tuple[str, ...]) -> str:
    if ":" in qname:
        return "AAAA"
    if addresses and all(":" in address for address in addresses):
        return "AAAA"
    return "A"


def _format_authority(host: str, port: int, default_port: int) -> str:
    rendered = f"[{host}]" if ":" in host else host
    return rendered if port == default_port else f"{rendered}:{port}"


def split_authority(authority: str, default_port: int) -> tuple[str, int]:
    """Split ``host[:port]`` including bracketed IPv6 literals."""
    value = authority.strip()
    if not value:
        raise ValueError("empty authority")
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            raise ValueError("unterminated IPv6 literal")
        host = value[1:end]
        rest = value[end + 1 :]
        if rest.startswith(":"):
            port = int(rest[1:])
        elif rest:
            raise ValueError("malformed authority")
        else:
            port = default_port
    elif value.count(":") > 1:
        host, port = value, default_port
    elif ":" in value:
        host, raw_port = value.rsplit(":", 1)
        port = int(raw_port)
    else:
        host, port = value, default_port
    if not host or not 0 < port < 65536:
        raise ValueError("malformed authority")
    return host.lower(), port


@dataclass(frozen=True)
class ProxyRequest:
    method: str
    host: str
    port: int
    path: str
    version: str
    headers: dict[str, str]
    body_length: int
    chunked: bool
    is_connect: bool


def parse_request(head: bytes) -> ProxyRequest:
    """Parse a client request head. Raises ``ValueError`` for malformed or unsupported input."""
    lines = head.decode("iso-8859-1").split("\r\n")
    if not lines or not lines[0]:
        raise ValueError("missing request line")
    parts = lines[0].split(" ")
    if len(parts) != 3:
        raise ValueError("malformed request line")
    method, target, version = parts
    if not _METHOD_TOKEN.fullmatch(method):
        raise ValueError("malformed method")
    if not version.startswith("HTTP/"):
        raise ValueError("malformed version")

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise ValueError("malformed header")
        headers[name.strip().lower()] = value.strip()

    chunked = headers.get("transfer-encoding", "").lower() == "chunked"
    raw_length = headers.get("content-length")
    body_length = 0
    if raw_length is not None:
        try:
            body_length = int(raw_length)
        except ValueError as exc:
            raise ValueError("malformed content-length") from exc
        if body_length < 0:
            raise ValueError("negative content-length")

    if method.upper() == "CONNECT":
        host, port = split_authority(target, 443)
        return ProxyRequest(
            method=method.upper(),
            host=host,
            port=port,
            path="",
            version=version,
            headers=headers,
            body_length=0,
            chunked=False,
            is_connect=True,
        )

    if "://" in target:
        split = urlsplit(target)
        if split.scheme != "http":
            raise ValueError("only http:// absolute-form requests are accepted; use CONNECT")
        if not split.hostname:
            raise ValueError("absolute-form request without host")
        host = split.hostname.lower()
        port = split.port or 80
        path = split.path or "/"
        if split.query:
            path = f"{path}?{split.query}"
    else:
        host_header = headers.get("host")
        if not host_header:
            raise ValueError("origin-form request without Host header")
        host, port = split_authority(host_header, 80)
        path = target if target.startswith("/") else f"/{target}"

    return ProxyRequest(
        method=method.upper(),
        host=host,
        port=port,
        path=path,
        version=version,
        headers=headers,
        body_length=body_length,
        chunked=chunked,
        is_connect=False,
    )


@dataclass(frozen=True)
class _Resolution:
    kind: Literal["ok", "blocked", "failed"]
    addresses: tuple[str, ...]


@dataclass(frozen=True)
class _RelayResult:
    status: int
    size: int
    truncated: bool


class _Truncated(Exception):
    def __init__(self, size: int) -> None:
        super().__init__("response body truncated")
        self.size = size


def _rewrite_response_head(
    head: bytes,
) -> tuple[int, bytes, int | None, bool]:
    """Return ``(status, rewritten head, content_length, chunked)`` for the client."""
    lines = head.decode("iso-8859-1").split("\r\n")
    status_parts = lines[0].split(" ", 2)
    if len(status_parts) < 2:
        raise ValueError("malformed status line")
    status = int(status_parts[1])
    output = [lines[0]]
    length: int | None = None
    chunked = False
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            continue
        lowered = name.strip().lower()
        if lowered in _HOP_BY_HOP:
            continue
        if lowered == "content-length":
            try:
                length = int(value.strip())
            except ValueError:
                length = None
        elif lowered == "transfer-encoding" and "chunked" in value.lower():
            chunked = True
        output.append(line)
    output.append("Connection: close")
    return status, ("\r\n".join(output) + "\r\n\r\n").encode("iso-8859-1"), length, chunked


async def _close_stream(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(ConnectionError, OSError, RuntimeError):
        await writer.wait_closed()


class EgressProxy:
    """A forward proxy bound to one address and one audit-log listener identity.

    Usage::

        async with EgressProxy(policy, port=0, log_dir=dir, trial_id="t-1") as proxy:
            host, port = proxy.address
    """

    def __init__(
        self,
        policy: Policy,
        *,
        log_dir: str | Path,
        trial_id: str,
        host: str = "127.0.0.1",
        port: int = 0,
        round: int = 0,
        process: str = "unknown",
        connect_timeout_s: float = 10.0,
        io_timeout_s: float = 30.0,
        audit_log: AuditLog | None = None,
    ) -> None:
        self._policy = policy
        self._host = host
        self._requested_port = port
        self._connect_timeout_s = connect_timeout_s
        self._io_timeout_s = io_timeout_s
        self._log = audit_log or AuditLog(log_dir, trial_id=trial_id, round=round, process=process)
        self._owns_log = audit_log is None
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._address: tuple[str, int] | None = None

    @property
    def policy(self) -> Policy:
        return self._policy

    @property
    def address(self) -> tuple[str, int]:
        if self._address is None:
            raise RuntimeError("proxy is not running")
        return self._address

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("proxy already started")
        self._server = await asyncio.start_server(
            self._accept, self._host, self._requested_port, limit=_HEAD_LIMIT
        )
        sockets = self._server.sockets or ()
        if not sockets:
            raise RuntimeError("proxy failed to bind")
        raw_host, raw_port = sockets[0].getsockname()[:2]
        self._address = (str(raw_host), int(raw_port))

    async def stop(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        if self._owns_log:
            self._log.close()
        self._address = None

    async def __aenter__(self) -> EgressProxy:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.stop()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.create_task(self._handle(reader, writer))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.CancelledError):
            pass
        except Exception as exc:  # pragma: no cover - defensive: report, keep the listener alive
            traceback.print_exception(exc, file=sys.stderr)
        finally:
            await _close_stream(writer)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await self._read_head(reader)
        except TimeoutError:
            await self._send_error(writer, 408, "request_timeout")
            return
        except asyncio.LimitOverrunError:
            await self._send_error(writer, 431, "headers_too_large")
            return
        except asyncio.IncompleteReadError:
            return
        try:
            request = parse_request(head)
        except ValueError:
            await self._send_error(writer, 400, "bad_request")
            return
        if request.is_connect:
            await self._handle_connect(request, reader, writer)
        else:
            await self._handle_http(request, reader, writer)

    async def _read_head(self, reader: asyncio.StreamReader) -> bytes:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self._io_timeout_s)

    async def _send(
        self, writer: asyncio.StreamWriter, status: int, reason: str, body: bytes
    ) -> int:
        head = (
            f"HTTP/1.1 {status} {_reason_phrase(status)}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("iso-8859-1")
        writer.write(head + body)
        await writer.drain()
        return len(body)

    async def _send_error(self, writer: asyncio.StreamWriter, status: int, reason: str) -> int:
        return await self._send(writer, status, reason, _error_body(status, reason))

    async def _deny(
        self,
        request: ProxyRequest,
        writer: asyncio.StreamWriter,
        reason: str,
        *,
        log_dns: bool = True,
    ) -> None:
        size = await self._send_error(writer, 403, reason)
        if log_dns:
            self._log.write_dns(
                qname=request.host,
                qtype=_qtype(request.host, ()),
                answer=(),
                blocked=True,
            )
        self._log.write_url(
            method=request.method,
            host=request.host,
            path=request.path,
            status=403,
            bytes_count=size,
            blocked_reason=reason,
        )

    async def _upstream_error(
        self, request: ProxyRequest, writer: asyncio.StreamWriter, reason: str
    ) -> None:
        size = await self._send_error(writer, 502, reason)
        self._log.write_url(
            method=request.method,
            host=request.host,
            path=request.path,
            status=502,
            bytes_count=size,
            blocked_reason=None,
        )

    async def _resolve(self, qname: str, port: int) -> _Resolution:
        loop = asyncio.get_running_loop()
        try:
            infos = await asyncio.wait_for(
                loop.getaddrinfo(qname, port, type=socket.SOCK_STREAM),
                self._connect_timeout_s,
            )
        except (OSError, TimeoutError):
            self._log.write_dns(qname=qname, qtype=_qtype(qname, ()), answer=(), blocked=False)
            return _Resolution("failed", ())
        addresses = tuple(dict.fromkeys(str(info[4][0]) for info in infos))
        if not addresses:
            self._log.write_dns(qname=qname, qtype=_qtype(qname, ()), answer=(), blocked=False)
            return _Resolution("failed", ())
        blocked_addresses = tuple(
            address for address in addresses if self._policy.is_blocked_address(address)
        )
        blocked = bool(addresses) and len(blocked_addresses) == len(addresses)
        self._log.write_dns(
            qname=qname,
            qtype=_qtype(qname, addresses),
            answer=addresses,
            blocked=blocked,
        )
        if blocked:
            return _Resolution("blocked", ())
        return _Resolution(
            "ok",
            tuple(address for address in addresses if not self._policy.is_blocked_address(address)),
        )

    async def _open(
        self, route: Route, address: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        context = ssl.create_default_context() if route.tls else None
        return await asyncio.wait_for(
            asyncio.open_connection(
                address,
                route.port,
                ssl=context,
                server_hostname=route.host if route.tls else None,
            ),
            self._connect_timeout_s,
        )

    async def _handle_http(
        self,
        request: ProxyRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        decision = self._policy.decide(
            method=request.method,
            host=request.host,
            port=request.port,
            path=request.path,
        )
        if not decision.allowed or decision.route is None:
            await self._deny(request, writer, decision.reason or "denied")
            return
        route = decision.route

        if request.chunked:
            size = await self._send_error(writer, 501, "chunked_not_supported")
            self._log.write_url(
                method=request.method,
                host=request.host,
                path=request.path,
                status=501,
                bytes_count=size,
                blocked_reason=None,
            )
            return

        resolution = await self._resolve(route.host, route.port)
        if resolution.kind == "blocked":
            await self._deny(request, writer, BLOCK_BLOCKED_CIDR, log_dns=False)
            return
        if resolution.kind == "failed":
            await self._upstream_error(request, writer, "dns_failure")
            return

        body = b""
        if request.body_length:
            try:
                body = await asyncio.wait_for(
                    reader.readexactly(request.body_length), self._io_timeout_s
                )
            except (TimeoutError, asyncio.IncompleteReadError):
                size = await self._send_error(writer, 400, "bad_request")
                self._log.write_url(
                    method=request.method,
                    host=request.host,
                    path=request.path,
                    status=400,
                    bytes_count=size,
                    blocked_reason=None,
                )
                return

        try:
            upstream_reader, upstream_writer = await self._open(route, resolution.addresses[0])
        except (OSError, TimeoutError):
            await self._upstream_error(request, writer, "connect_failed")
            return

        result: _RelayResult | None = None
        try:
            await self._forward_request(request, route, body, upstream_writer)
            result = await self._relay_response(request, upstream_reader, writer)
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError):
            result = None
        finally:
            await _close_stream(upstream_writer)

        if result is None:
            await self._upstream_error(request, writer, "upstream_error")
            return
        self._log.write_url(
            method=request.method,
            host=request.host,
            path=request.path,
            status=result.status,
            bytes_count=result.size,
            blocked_reason=None,
        )

    async def _forward_request(
        self,
        request: ProxyRequest,
        route: Route,
        body: bytes,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        default_port = 443 if route.tls else 80
        lines = [
            f"{request.method} {route.path} HTTP/1.1",
            f"Host: {_format_authority(route.host, route.port, default_port)}",
        ]
        for name, value in request.headers.items():
            if name in _HOP_BY_HOP or name in ("host", "content-length"):
                continue
            lines.append(f"{name}: {value}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        lines.append("Connection: close")
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("iso-8859-1")
        upstream_writer.write(head + body)
        await upstream_writer.drain()

    async def _relay_response(
        self,
        request: ProxyRequest,
        upstream_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> _RelayResult:
        head = await self._read_head(upstream_reader)
        status, rewritten, length, chunked = _rewrite_response_head(head)
        client_writer.write(rewritten)
        await client_writer.drain()
        if request.method == "HEAD" or status in (204, 304) or 100 <= status < 200:
            return _RelayResult(status, 0, False)
        try:
            if chunked:
                size = await self._relay_chunked(upstream_reader, client_writer)
            elif length is not None:
                size = await self._relay_exact(upstream_reader, client_writer, length)
            else:
                size = await self._relay_until_eof(upstream_reader, client_writer)
        except _Truncated as truncated:
            return _RelayResult(status, truncated.size, True)
        return _RelayResult(status, size, False)

    async def _read_exact(self, reader: asyncio.StreamReader, size: int) -> bytes:
        return await asyncio.wait_for(reader.readexactly(size), self._io_timeout_s)

    async def _read_line(self, reader: asyncio.StreamReader) -> bytes:
        return await asyncio.wait_for(reader.readuntil(b"\r\n"), self._io_timeout_s)

    async def _relay_exact(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, length: int
    ) -> int:
        total = 0
        try:
            while total < length:
                data = await self._read_exact(reader, min(_CHUNK, length - total))
                writer.write(data)
                await writer.drain()
                total += len(data)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise _Truncated(total) from exc
        return total

    async def _relay_chunked(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> int:
        total = 0
        try:
            while True:
                line = await self._read_line(reader)
                writer.write(line)
                await writer.drain()
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    while True:
                        trailer = await self._read_line(reader)
                        writer.write(trailer)
                        if trailer == b"\r\n":
                            break
                    await writer.drain()
                    return total
                data = await self._read_exact(reader, size + 2)
                writer.write(data)
                await writer.drain()
                total += size
        except (OSError, TimeoutError, ValueError, asyncio.IncompleteReadError) as exc:
            raise _Truncated(total) from exc

    async def _relay_until_eof(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> int:
        total = 0
        try:
            while True:
                data = await asyncio.wait_for(reader.read(_CHUNK), self._io_timeout_s)
                if not data:
                    return total
                writer.write(data)
                await writer.drain()
                total += len(data)
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            raise _Truncated(total) from exc

    async def _handle_connect(
        self,
        request: ProxyRequest,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        decision = self._policy.decide(
            method="CONNECT",
            host=request.host,
            port=request.port,
            path="",
            is_connect=True,
        )
        if not decision.allowed or decision.route is None:
            await self._deny(request, writer, decision.reason or "denied")
            return
        route = decision.route

        resolution = await self._resolve(route.host, route.port)
        if resolution.kind == "blocked":
            await self._deny(request, writer, BLOCK_BLOCKED_CIDR, log_dns=False)
            return
        if resolution.kind == "failed":
            await self._upstream_error(request, writer, "dns_failure")
            return

        try:
            upstream_reader, upstream_writer = await self._open(route, resolution.addresses[0])
        except (OSError, TimeoutError):
            await self._upstream_error(request, writer, "connect_failed")
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        client_to_upstream = asyncio.create_task(self._pump(reader, upstream_writer))
        upstream_to_client = asyncio.create_task(self._pump(upstream_reader, writer))
        size = 0
        try:
            done, _ = await asyncio.wait(
                {client_to_upstream, upstream_to_client},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if upstream_to_client not in done:
                if client_to_upstream.exception() is None:
                    # The client finished its request; give the upstream a bounded chance to
                    # finish responding before tearing the tunnel down.
                    with contextlib.suppress(OSError, RuntimeError):
                        upstream_writer.write_eof()
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(upstream_to_client), self._io_timeout_s
                        )
                    except TimeoutError:
                        upstream_to_client.cancel()
                else:
                    upstream_to_client.cancel()
            client_to_upstream.cancel()
            results = await asyncio.gather(
                client_to_upstream, upstream_to_client, return_exceptions=True
            )
            forwarded = results[1]
            if isinstance(forwarded, int):
                size = forwarded
        finally:
            await _close_stream(upstream_writer)

        self._log.write_url(
            method="CONNECT",
            host=request.host,
            path="",
            status=200,
            bytes_count=size,
            blocked_reason=None,
        )

    @staticmethod
    async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> int:
        total = 0
        while True:
            data = await reader.read(_CHUNK)
            if not data:
                return total
            writer.write(data)
            await writer.drain()
            total += len(data)
