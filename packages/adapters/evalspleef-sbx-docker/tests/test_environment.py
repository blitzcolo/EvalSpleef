"""DockerProvider.environment() against the real local daemon.

Expected values come from the ``docker`` CLI and the Python ``platform`` module, not
from the adapter, so a wrong field fails here.
"""

from __future__ import annotations

import asyncio
import contextlib
import platform
import subprocess
from collections.abc import AsyncIterator

import pytest
from evalspleef_sbx_docker.provider import DockerProvider

from evalspleef.spi.environment import MachineFacts, normalise_arch, os_family


def _docker(command: str, template: str) -> str:
    argv = ["docker", "context", "inspect"] if command == "context" else ["docker", command]
    result = subprocess.run(
        [*argv, "--format", template],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


async def test_a_local_engine_reports_host_runtime_and_guest(provider: DockerProvider) -> None:
    report = await provider.environment()

    assert report.reported
    assert report.runtime.name == "docker"
    assert report.runtime.version == _docker("version", "{{.Server.Version}}")
    assert report.guest.kernel == _docker("info", "{{.KernelVersion}}")
    assert report.guest.cpu_count == int(_docker("info", "{{.NCPU}}"))
    assert report.guest.memory_bytes == int(_docker("info", "{{.MemTotal}}"))
    assert report.guest.os == "linux"
    # The engine listens on a local socket, so the host is this machine.
    assert report.extra["engine_endpoint"] == "local"
    assert report.host.os == os_family(platform.system())
    assert report.host.kernel == platform.release()
    assert report.host.arch == normalise_arch(platform.machine())
    assert report.host.memory_bytes is not None and report.host.memory_bytes > 0
    assert report.extra["oci_runtime"] == "runc"


async def test_identical_engines_report_identical_environments(provider: DockerProvider) -> None:
    assert await provider.environment() == await provider.environment()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass  # the other side hung up; the bridge only has to carry complete requests
    finally:
        writer.close()


@contextlib.asynccontextmanager
async def _tcp_bridge(socket_path: str) -> AsyncIterator[int]:
    """A loopback TCP endpoint in front of the local engine socket: a stand-in remote engine."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream_reader, upstream_writer = await asyncio.open_unix_connection(socket_path)
        await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def test_an_engine_reached_over_tcp_never_reports_this_machine_as_its_host(
    provider: DockerProvider,
) -> None:
    socket_url = _docker("context", "{{.Endpoints.docker.Host}}")
    if not socket_url.startswith("unix://"):
        pytest.skip("the local engine is not on a unix socket")
    async with _tcp_bridge(socket_url.removeprefix("unix://")) as port:
        remote = DockerProvider(
            "remote-test", {"engine_url": f"tcp://127.0.0.1:{port}"}, provider.secrets
        )
        try:
            report = await remote.environment()
        finally:
            await remote.close()

    assert report.extra["engine_endpoint"] == "remote"
    assert report.host == MachineFacts()
    # The engine's own facts still come through.
    assert report.runtime.version == _docker("version", "{{.Server.Version}}")
    assert report.guest.kernel == _docker("info", "{{.KernelVersion}}")
