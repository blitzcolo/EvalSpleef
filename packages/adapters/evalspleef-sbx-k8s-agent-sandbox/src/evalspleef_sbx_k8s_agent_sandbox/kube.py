"""The thin Kubernetes surface the provider needs (TECH-DESIGN section 7.4.8).

Only six operations are used: create/get/list/delete custom objects, exec in a
pod with streaming, and copy files in or out of a pod. ``RealKubeApi`` implements
them on the official ``kubernetes`` Python client, imported lazily so the
package works without the optional ``k8s`` extra. ``FakeKubeApi`` (``fake.py``)
implements the same protocol in memory for tests.

Exec, timeouts and file copies go through pod exec. A real cluster is not
available in this repository, so the ``RealKubeApi`` paths are not verified on a
real host; see the package README.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import posixpath
import socket
import tarfile
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from evalspleef.spi.errors import SandboxLost, SpleefError, TransferError
from evalspleef.spi.sandbox import TIMEOUT_EXIT_CODE, ExecResult, OutputCallback

from ._tar import build_upload_tar, extract_download_tar
from .output import CappedBuffer

SANDBOX_GROUP = "agents.x-k8s.io"
EXTENSIONS_GROUP = "extensions.agents.x-k8s.io"
API_VERSION = "v1beta1"
SANDBOX_PLURAL = "sandboxes"
SANDBOX_TEMPLATE_PLURAL = "sandboxtemplates"
SANDBOX_WARMPOOL_PLURAL = "sandboxwarmpools"
SANDBOX_CLAIM_PLURAL = "sandboxclaims"

_EXEC_TIMEOUT_GRACE_S = 15.0
_COPY_CAP = 1 << 30


@dataclass(frozen=True)
class ExecRequest:
    """One pod exec call, with the SPI's streaming and timeout semantics."""

    namespace: str
    pod: str
    container: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str | None
    stdin: bytes | None
    timeout_s: float | None
    on_output: OutputCallback | None
    max_output_bytes: int


class KubeApi(Protocol):
    """The Kubernetes operations used by the agent-sandbox provider."""

    async def create_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    async def get_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None: ...

    async def delete_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        name: str,
    ) -> None: ...

    async def list_objects(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        label_selector: str,
    ) -> list[dict[str, Any]]: ...

    async def exec_in_pod(self, request: ExecRequest) -> ExecResult: ...

    async def copy_to_pod(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        source: Path,
        destination: str,
    ) -> None: ...

    async def copy_from_pod(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        source: str,
        destination: Path,
        max_bytes: int | None,
    ) -> None: ...

    async def close(self) -> None: ...


class RealKubeApi:
    """``KubeApi`` on the kubernetes Python client (optional extra ``k8s``)."""

    def __init__(
        self,
        *,
        context: str | None = None,
        kubeconfig: str | None = None,
    ) -> None:
        self._context = context
        self._kubeconfig = kubeconfig
        self._custom: Any = None
        self._core: Any = None
        self._api_client: Any = None

    def _clients(self) -> tuple[Any, Any]:
        if self._custom is not None and self._core is not None:
            return self._custom, self._core
        client = importlib.import_module("kubernetes.client")
        config = importlib.import_module("kubernetes.config")
        if self._api_client is None:
            if self._kubeconfig is not None:
                config.load_kube_config(
                    config_file=self._kubeconfig,
                    context=self._context,
                )
            else:
                try:
                    config.load_incluster_config()
                except config.ConfigException:
                    config.load_kube_config(context=self._context)
            self._api_client = client.ApiClient()
        self._custom = client.CustomObjectsApi(self._api_client)
        self._core = client.CoreV1Api(self._api_client)
        return self._custom, self._core

    async def create_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        body: Mapping[str, Any],
    ) -> dict[str, Any]:
        custom, _ = self._clients()
        try:
            created = await asyncio.to_thread(
                custom.create_namespaced_custom_object,
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
                body=dict(body),
            )
        except Exception as exc:
            raise SpleefError(f"kubernetes create {plural}/{_name(body)} failed: {exc}") from exc
        return dict(created)

    async def get_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        name: str,
    ) -> dict[str, Any] | None:
        custom, _ = self._clients()
        try:
            found = await asyncio.to_thread(
                custom.get_namespaced_custom_object,
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
                name=name,
            )
        except Exception as exc:
            if _status(exc) == 404:
                return None
            raise SpleefError(f"kubernetes get {plural}/{name} failed: {exc}") from exc
        return dict(found)

    async def delete_object(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        name: str,
    ) -> None:
        custom, _ = self._clients()
        try:
            await asyncio.to_thread(
                custom.delete_namespaced_custom_object,
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
                name=name,
            )
        except Exception as exc:
            if _status(exc) == 404:
                return
            raise SpleefError(f"kubernetes delete {plural}/{name} failed: {exc}") from exc

    async def list_objects(
        self,
        *,
        group: str,
        version: str,
        plural: str,
        namespace: str,
        label_selector: str,
    ) -> list[dict[str, Any]]:
        custom, _ = self._clients()
        try:
            response = await asyncio.to_thread(
                custom.list_namespaced_custom_object,
                group=group,
                version=version,
                namespace=namespace,
                plural=plural,
                label_selector=label_selector,
            )
        except Exception as exc:
            raise SpleefError(f"kubernetes list {plural} failed: {exc}") from exc
        items = response.get("items", [])
        return [dict(item) for item in items]

    async def exec_in_pod(self, request: ExecRequest) -> ExecResult:
        return await asyncio.to_thread(self._exec_blocking, request)

    def _exec_blocking(self, request: ExecRequest) -> ExecResult:
        _, core = self._clients()
        stream_module = importlib.import_module("kubernetes.stream")
        argv = _exec_argv(request)
        stdout = CappedBuffer(request.max_output_bytes)
        stderr = CappedBuffer(request.max_output_bytes)
        started = time.monotonic()
        timed_out = False
        try:
            response = stream_module.stream(
                core.connect_get_namespaced_pod_exec,
                request.pod,
                request.namespace,
                container=request.container,
                command=argv,
                stderr=True,
                stdin=request.stdin is not None,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
        except Exception as exc:
            if _status(exc) == 404:
                raise SandboxLost(
                    detail={"sandbox_id": request.pod, "cause": "pod not found"}
                ) from exc
            raise SpleefError(f"kubernetes exec in {request.pod} failed: {exc}") from exc
        deadline = (
            None
            if request.timeout_s is None
            else started + request.timeout_s + _EXEC_TIMEOUT_GRACE_S
        )
        try:
            if request.stdin is not None:
                response.write_stdin(request.stdin)
                with contextlib.suppress(Exception):
                    response.sock.shutdown(socket.SHUT_WR)
            while response.is_open():
                if deadline is not None and time.monotonic() > deadline:
                    timed_out = True
                    response.close()
                    break
                response.update(timeout=0.5)
                if response.peek_stdout():
                    chunk = response.read_stdout().encode("utf-8", errors="replace")
                    if chunk:
                        stdout.append(chunk)
                        if request.on_output is not None:
                            request.on_output("stdout", chunk)
                if response.peek_stderr():
                    chunk = response.read_stderr().encode("utf-8", errors="replace")
                    if chunk:
                        stderr.append(chunk)
                        if request.on_output is not None:
                            request.on_output("stderr", chunk)
            exit_code = response.returncode
            if not isinstance(exit_code, int):
                exit_code = 1
        finally:
            response.close()
        if timed_out:
            exit_code = TIMEOUT_EXIT_CODE
        elif request.timeout_s is not None and exit_code == TIMEOUT_EXIT_CODE:
            timed_out = True
        return ExecResult(
            exit_code=exit_code,
            stdout=stdout.bytes(),
            stderr=stderr.bytes(),
            timed_out=timed_out,
            duration_s=time.monotonic() - started,
            truncated=stdout.truncated or stderr.truncated,
        )

    async def copy_to_pod(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        source: Path,
        destination: str,
    ) -> None:
        remote = _absolute(destination)
        dest_dir = posixpath.dirname(remote) or "/"
        made = await self.exec_in_pod(
            ExecRequest(
                namespace=namespace,
                pod=pod,
                container=container,
                argv=("mkdir", "-p", dest_dir),
                env={},
                cwd=None,
                stdin=None,
                timeout_s=60.0,
                on_output=None,
                max_output_bytes=1024,
            )
        )
        if made.exit_code != 0:
            raise TransferError(f"cannot create remote directory {dest_dir!r}")
        payload = build_upload_tar(source, arcname=posixpath.basename(remote))
        result = await self.exec_in_pod(
            ExecRequest(
                namespace=namespace,
                pod=pod,
                container=container,
                argv=("tar", "-xf", "-", "-C", dest_dir),
                env={},
                cwd=None,
                stdin=payload,
                timeout_s=300.0,
                on_output=None,
                max_output_bytes=4096,
            )
        )
        if result.exit_code != 0:
            raise TransferError(
                f"upload to {destination!r} failed (exit {result.exit_code}): {result.stderr!r}"
            )

    async def copy_from_pod(
        self,
        *,
        namespace: str,
        pod: str,
        container: str,
        source: str,
        destination: Path,
        max_bytes: int | None,
    ) -> None:
        remote = _absolute(source)
        remote_dir = posixpath.dirname(remote) or "/"
        cap = _COPY_CAP if max_bytes is None else max_bytes + (1 << 20)
        with tempfile.TemporaryFile(prefix="spleef-k8s-download-") as spool:
            written = 0

            def sink(channel: str, data: bytes) -> None:
                nonlocal written
                if channel != "stdout":
                    return
                written += len(data)
                if written > cap:
                    raise TransferError(f"download of {source!r} exceeds the size cap")
                spool.write(data)

            result = await self.exec_in_pod(
                ExecRequest(
                    namespace=namespace,
                    pod=pod,
                    container=container,
                    argv=("tar", "-cf", "-", "-C", remote_dir, posixpath.basename(remote)),
                    env={},
                    cwd=None,
                    stdin=None,
                    timeout_s=300.0,
                    on_output=sink,
                    max_output_bytes=1 << 20,
                )
            )
            if result.exit_code != 0:
                raise TransferError(
                    f"download of {source!r} failed (exit {result.exit_code}): {result.stderr!r}"
                )
            spool.seek(0)
            try:
                with tarfile.open(fileobj=spool, mode="r:") as archive:
                    extract_download_tar(archive, destination, max_bytes=max_bytes)
            except TransferError:
                raise
            except Exception as exc:
                raise TransferError(f"failed to unpack {source!r}: {exc}") from exc

    async def close(self) -> None:
        if self._api_client is not None:
            await asyncio.to_thread(self._api_client.close)
            self._api_client = None
            self._custom = None
            self._core = None


def _exec_argv(request: ExecRequest) -> list[str]:
    argv = list(request.argv)
    if request.env:
        argv = [
            "/usr/bin/env",
            *[f"{key}={value}" for key, value in request.env.items()],
            *argv,
        ]
    if request.cwd is not None:
        argv = [
            "/bin/sh",
            "-c",
            'cd "$1" || exit 125; shift; exec "$@"',
            "sh",
            request.cwd,
            *argv,
        ]
    if request.timeout_s is not None:
        milliseconds = f"{request.timeout_s}s"
        argv = ["timeout", "--kill-after=5s", milliseconds, *argv]
    return argv


def _absolute(remote: str) -> str:
    if not remote.startswith("/"):
        raise TransferError(f"remote path must be absolute, got {remote!r}")
    return posixpath.normpath(remote)


def _name(body: Mapping[str, Any]) -> str:
    metadata = body.get("metadata")
    if isinstance(metadata, Mapping):
        return str(metadata.get("name", "?"))
    return "?"


def _status(exc: Exception) -> int | None:
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None
