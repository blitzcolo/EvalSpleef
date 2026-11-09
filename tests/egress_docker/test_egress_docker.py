"""E1-E4: the Docker adapter really routes proxied network modes through the egress proxy."""

from __future__ import annotations

import json
from typing import Any

from evalspleef.spi.caps import NetMode

_HTTP_GET = """
import json, sys, urllib.error, urllib.request
try:
    response = urllib.request.urlopen(sys.argv[1], timeout=10)
    out = {"status": response.status, "body": response.read().decode()}
except urllib.error.HTTPError as exc:
    out = {"status": exc.code, "body": exc.read().decode()}
except Exception as exc:
    out = {"status": None, "error": repr(exc)}
print(json.dumps(out))
"""

_RAW_CONNECT = """
import json, socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close()
    out = {"connected": True}
except OSError as exc:
    out = {"connected": False, "errno": exc.errno, "error": repr(exc)}
print(json.dumps(out))
"""

_CONNECT_TUNNEL = """
import json, socket, sys
sock = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=10)
sock.sendall(b"CONNECT docs.test:80 HTTP/1.1\\r\\nHost: docs.test:80\\r\\n\\r\\n")
head = b""
while b"\\r\\n\\r\\n" not in head:
    head += sock.recv(4096)
status = head.split(b" ")[1].decode()
body = b""
if status == "200":
    sock.sendall(b"GET / HTTP/1.0\\r\\nHost: docs.test\\r\\n\\r\\n")
    while chunk := sock.recv(4096):
        body += chunk
print(json.dumps({"status": status, "body": body.decode()}))
"""

# Linux errno values inside the sandbox (ENETUNREACH, EHOSTUNREACH); the host may be macOS.
_NO_ROUTE = {101, 113}


async def _run_json(sandbox: Any, script: str, *args: str) -> dict[str, Any]:
    result = await sandbox.exec(["python", "-c", script, *args], timeout_s=30)
    assert result.exit_code == 0, result.stderr.decode()
    return json.loads(result.stdout.decode())


def test_the_shipped_proxy_image_is_what_ran(topology: Any) -> None:
    assert topology.proxy_image_built, (
        "deploy/docker/egress.Dockerfile did not build, so every test in this module ran the "
        "proxy from the bind-mount fallback instead of the shipped image"
    )


async def test_e1_allowlisted_host_is_served_through_the_proxy(
    make_sandbox: Any, topology: Any
) -> None:
    sandbox = await make_sandbox(NetMode.DOCS_WHITELIST, via_proxy=topology.proxy_via)
    reply = await _run_json(sandbox, _HTTP_GET, "http://docs.test/")
    assert reply["status"] == 200, reply
    assert reply["body"] == topology.docs_marker


async def test_e1_via_proxy_url_form_is_equivalent(make_sandbox: Any, topology: Any) -> None:
    sandbox = await make_sandbox(NetMode.DOCS_WHITELIST, via_proxy=f"http://{topology.proxy_via}")
    reply = await _run_json(sandbox, _HTTP_GET, "http://docs.test/")
    assert reply["status"] == 200, reply
    assert reply["body"] == topology.docs_marker


async def test_e2_blocked_host_is_denied_and_audited(make_sandbox: Any, topology: Any) -> None:
    sandbox = await make_sandbox(NetMode.DOCS_WHITELIST, via_proxy=topology.proxy_via)
    reply = await _run_json(sandbox, _HTTP_GET, "http://blocked.test/")
    assert reply["status"] == 403, reply
    assert topology.blocked_marker not in reply["body"]
    reason = json.loads(reply["body"])["blocked_reason"]
    assert reason
    lines = [row for row in topology.proxy_url_log() if row["host"] == "blocked.test"]
    assert lines, "proxy url.jsonl has no line for blocked.test"
    assert lines[-1]["status"] == 403
    assert lines[-1]["blocked_reason"] == reason


async def test_e3_direct_connection_bypassing_the_proxy_has_no_route(
    make_sandbox: Any, topology: Any
) -> None:
    sandbox = await make_sandbox(NetMode.DOCS_WHITELIST, via_proxy=topology.proxy_via)
    for target in (topology.origin_ip, topology.origin_bridge_ip):
        reply = await _run_json(sandbox, _RAW_CONNECT, target, "80")
        assert reply["connected"] is False, f"direct connection to {target} succeeded"
        assert reply["errno"] in _NO_ROUTE, reply
    by_name = await _run_json(
        sandbox,
        _HTTP_GET.replace("urlopen(", "build_opener(urllib.request.ProxyHandler({})).open("),
        "http://docs.test/",
    )
    assert by_name["status"] is None, by_name


async def test_e3b_connect_tunnel_to_allowlisted_host(make_sandbox: Any, topology: Any) -> None:
    sandbox = await make_sandbox(NetMode.DOCS_WHITELIST, via_proxy=topology.proxy_via)
    host, port = topology.proxy_via.split(":")
    reply = await _run_json(sandbox, _CONNECT_TUNNEL, host, port)
    assert reply["status"] == "200", reply
    assert topology.docs_marker in reply["body"]


async def test_e4_off_mode_still_has_no_network(make_sandbox: Any, topology: Any) -> None:
    sandbox = await make_sandbox(NetMode.OFF)
    for target in (topology.origin_ip, topology.origin_bridge_ip):
        reply = await _run_json(sandbox, _RAW_CONNECT, target, "80")
        assert reply["connected"] is False
    interfaces = await sandbox.exec(
        ["python", "-c", "import socket; print(*[n for _, n in socket.if_nameindex()])"]
    )
    names = interfaces.stdout.decode().split()
    # Docker Desktop's kernel also lists unconfigured tunnel devices (tunl0, ...); the
    # container must have no ethernet interface, which is what a joined network adds.
    assert "lo" in names
    assert not [name for name in names if name.startswith("eth")], names
    env = await sandbox.exec(["env"])
    assert "proxy" not in env.stdout.decode().lower()


async def test_e4_full_mode_is_unchanged(make_sandbox: Any, topology: Any) -> None:
    sandbox = await make_sandbox(NetMode.FULL)
    reply = await _run_json(sandbox, _RAW_CONNECT, topology.origin_bridge_ip, "80")
    assert reply["connected"] is True, reply
    env = await sandbox.exec(["env"])
    assert "proxy" not in env.stdout.decode().lower()
