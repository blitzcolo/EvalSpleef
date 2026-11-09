"""Real-Docker topology for the egress tests.

Layout (all objects carry the ``spleef-egresslane`` label and name prefix):

    sandbox --(internal net)--> proxy --(origin net)--> docs.test / blocked.test

The internal network has no gateway, so the proxy is the sandbox's only route out. The proxy
runs from ``spleef-egresslane-proxy:test`` when it can be built from
``deploy/docker/egress.Dockerfile``; without registry access that build cannot resolve its
base images, so the fixture falls back to the workspace packages bind-mounted read-only into
a ``python:3.12-slim`` container.
"""

from __future__ import annotations

import json
import queue
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from evalspleef_sbx_docker.provider import DockerProvider

from evalspleef.spi.caps import Arch, NetMode, OSFamily
from evalspleef.spi.sandbox import NetworkPolicy, Resources, Sandbox, SandboxSpec, TemplateRef

PREFIX = "spleef-egresslane"
LABEL = f"{PREFIX}=1"
BASE_IMAGE = "python:3.12-slim"
PROXY_IMAGE = f"{PREFIX}-proxy:test"
SANDBOX_IMAGE = f"{PREFIX}-sandbox:test"
PROXY_ALIAS = "egress-proxy"
PROXY_PORT = 3128
DOCS_MARKER = "docs-test-marker"
BLOCKED_MARKER = "blocked-test-marker"
ROOT = Path(__file__).resolve().parents[2]

_ORIGIN_SERVER = """
import http.server, sys
marker = sys.argv[1].encode()
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(marker)))
        self.end_headers()
        self.wfile.write(marker)
    def log_message(self, *args):
        pass
server = http.server.ThreadingHTTPServer(("0.0.0.0", 80), Handler)
print("origin-ready", flush=True)
server.serve_forever()
"""

_SANDBOX_DOCKERFILE = f"""\
FROM {BASE_IMAGE}
RUN useradd --create-home --uid 1000 --shell /bin/sh agent \
 && mkdir -p /workspace && chown -R agent:agent /workspace /home/agent
USER agent
"""


def docker(*args: str, check: bool = True, timeout: float = 120) -> str:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args)} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def wait_for_log(container: str, needle: str, timeout_s: float = 60) -> None:
    """Block until ``needle`` shows up in the container log; no sleeping, bounded wait."""
    process = subprocess.Popen(
        ["docker", "logs", "--follow", container],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    seen: list[str] = []
    try:
        while True:
            try:
                line = lines.get(timeout=timeout_s)
            except queue.Empty:
                raise TimeoutError(f"{container}: no {needle!r} in log: {seen}") from None
            if line is None:
                raise RuntimeError(f"{container} exited before {needle!r}: {seen}")
            seen.append(line)
            if needle in line:
                return
    finally:
        process.kill()
        process.wait()


def container_ip(container: str, network: str) -> str:
    template = '{{(index .NetworkSettings.Networks "' + network + '").IPAddress}}'
    return docker("inspect", "-f", template, container).strip()


@dataclass(frozen=True)
class Topology:
    internal_net: str
    origin_net: str
    proxy: str
    proxy_via: str
    origin_ip: str  # docs.test on the origin network
    origin_bridge_ip: str  # docs.test on the default bridge, a path that skips the proxy
    proxy_image_built: bool
    docs_marker: str = DOCS_MARKER
    blocked_marker: str = BLOCKED_MARKER

    def proxy_url_log(self) -> list[dict[str, Any]]:
        text = docker("exec", self.proxy, "cat", "/tmp/url.jsonl", check=False)
        return [json.loads(line) for line in text.splitlines() if line.strip()]


def _try_build_proxy_image() -> bool:
    result = subprocess.run(
        [
            "docker", "build", "--label", LABEL, "-t", PROXY_IMAGE,
            "-f", str(ROOT / "deploy/docker/egress.Dockerfile"), str(ROOT),
        ],
        capture_output=True, text=True, timeout=600, check=False,
    )  # fmt: skip
    return result.returncode == 0


def _remove_lane_objects() -> None:
    containers = docker("ps", "-aq", "--filter", f"label={LABEL}", check=False).split()
    if containers:
        docker("rm", "-f", *containers, check=False)
    networks = docker("network", "ls", "-q", "--filter", f"label={LABEL}", check=False).split()
    if networks:
        docker("network", "rm", *networks, check=False)
    for image in (PROXY_IMAGE, SANDBOX_IMAGE):
        docker("rmi", "-f", image, check=False)


@pytest.fixture(scope="session")
def topology() -> Iterator[Topology]:
    if not docker_available():
        pytest.skip("no working local Docker daemon")
    run_id = uuid.uuid4().hex[:8]
    workdir = Path(tempfile.mkdtemp(prefix=f"{PREFIX}-"))
    try:
        if subprocess.run(
            ["docker", "image", "inspect", BASE_IMAGE], capture_output=True, check=False
        ).returncode:
            docker("pull", BASE_IMAGE, timeout=600)
        (workdir / "sandbox").mkdir()
        (workdir / "sandbox" / "Dockerfile").write_text(_SANDBOX_DOCKERFILE, encoding="utf-8")
        docker(
            "build", "--label", LABEL, "-t", SANDBOX_IMAGE, str(workdir / "sandbox"), timeout=600
        )

        internal_net = f"{PREFIX}-internal-{run_id}"
        origin_net = f"{PREFIX}-origin-{run_id}"
        docker("network", "create", "--internal", "--label", LABEL, internal_net)
        docker("network", "create", "--label", LABEL, origin_net)

        origins: dict[str, str] = {}
        for host, marker in (("docs.test", DOCS_MARKER), ("blocked.test", BLOCKED_MARKER)):
            name = f"{PREFIX}-{host.split('.')[0]}-{run_id}"
            docker(
                "run", "-d", "--name", name, "--label", LABEL, "--network", origin_net,
                "--network-alias", host, BASE_IMAGE, "python", "-u", "-c", _ORIGIN_SERVER, marker,
            )  # fmt: skip
            origins[host] = name
        # docs.test is also on the default bridge: a direct path that skips the proxy.
        docker("network", "connect", "bridge", origins["docs.test"])
        for name in origins.values():
            wait_for_log(name, "origin-ready")

        policy_dir = workdir / "policy"
        policy_dir.mkdir()
        (policy_dir / "policy.yaml").write_text(
            yaml.safe_dump({"mode": "docs_whitelist", "allow": ["docs.test"]}), encoding="utf-8"
        )
        proxy_name = f"{PREFIX}-proxy-{run_id}"
        proxy_built = _try_build_proxy_image()
        listen = f"0.0.0.0:{PROXY_PORT}"
        if proxy_built:
            run_args = [
                PROXY_IMAGE, "--policy", "/policy/policy.yaml", "--listen", listen,
                "--log-dir", "/tmp", "--trial-id", "egress-docker",
            ]  # fmt: skip
            mounts = ["-v", f"{policy_dir}:/policy:ro"]
        else:
            deps = workdir / "deps"
            deps.mkdir()
            shutil.copytree(Path(yaml.__file__).parent, deps / "yaml", ignore=_skip_native)
            mounts = [
                "-v", f"{policy_dir}:/policy:ro",
                "-v", f"{ROOT / 'packages/evalspleef-egress/src'}:/opt/egress-src:ro",
                "-v", f"{ROOT / 'packages/evalspleef-spi/src'}:/opt/spi-src:ro",
                "-v", f"{deps}:/opt/deps:ro",
                "-e", "PYTHONPATH=/opt/egress-src:/opt/spi-src:/opt/deps",
            ]  # fmt: skip
            run_args = [
                BASE_IMAGE, "python", "-u", "-c",
                "import sys; from evalspleef.egress.cli import main; main(sys.argv[1:])",
                "--policy", "/policy/policy.yaml", "--listen", listen,
                "--log-dir", "/tmp", "--trial-id", "egress-docker",
            ]  # fmt: skip
        docker(
            "run", "-d", "--name", proxy_name, "--label", LABEL, "--network", origin_net,
            *mounts, *run_args,
        )  # fmt: skip
        docker("network", "connect", "--alias", PROXY_ALIAS, internal_net, proxy_name)
        wait_for_log(proxy_name, "spleef-egress listening")

        yield Topology(
            internal_net=internal_net,
            origin_net=origin_net,
            proxy=proxy_name,
            proxy_via=f"{PROXY_ALIAS}:{PROXY_PORT}",
            origin_ip=container_ip(origins["docs.test"], origin_net),
            origin_bridge_ip=container_ip(origins["docs.test"], "bridge"),
            proxy_image_built=proxy_built,
        )
    finally:
        _remove_lane_objects()
        shutil.rmtree(workdir, ignore_errors=True)


def _skip_native(_directory: str, names: list[str]) -> list[str]:
    return [name for name in names if name.endswith((".so", ".pyc")) or name == "__pycache__"]


class _Secrets:
    def resolve(self, ref: str) -> str:
        raise RuntimeError(f"unexpected secret resolution: {ref!r}")


@pytest.fixture
async def provider(topology: Topology) -> AsyncIterator[DockerProvider]:
    instance = DockerProvider(
        "egresslane",
        {
            "egress_network": topology.internal_net,
            "max_concurrency": 4,
            "pids_limit": 64,
            "ttl_sweep_interval_s": 3600,
        },
        _Secrets(),
    )
    try:
        yield instance
    finally:
        await instance.close()


@pytest.fixture
async def make_sandbox(
    provider: DockerProvider, topology: Topology
) -> AsyncIterator[Callable[..., Any]]:
    created: list[Sandbox] = []

    async def make(mode: NetMode, *, via_proxy: str | None = None) -> Sandbox:
        trial = f"egress-{uuid.uuid4().hex[:8]}"
        spec = SandboxSpec(
            trial_id=trial,
            lease_id=f"lease-{trial}",
            role="agent",
            os=OSFamily.LINUX,
            arch=Arch.ARM64,
            template=TemplateRef(kind="oci", ref=SANDBOX_IMAGE, digest="sha256:" + "0" * 64),
            resources=Resources(cpus=1, memory_mb=256, disk_mb=256),
            network=NetworkPolicy(mode=mode, via_proxy=via_proxy),
            max_lifetime_s=600,
            labels={PREFIX: "1", "trial": trial},
        )
        sandbox = await provider.create(spec)
        created.append(sandbox)
        return sandbox

    try:
        yield make
    finally:
        for sandbox in created:
            try:
                await sandbox.destroy()
            except Exception:
                continue  # the session teardown removes anything left by label
