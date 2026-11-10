"""One live agent-sandbox instance.

Exec and file copies go through the ``KubeApi`` transport; the sandbox carries
the SPI spec, the generated object name and whether it was claimed from a warm
pool (a claimed instance is never returned to the pool).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from evalspleef.spi.errors import CapabilityNotSupported, SandboxLost, TransferError
from evalspleef.spi.sandbox import ExecResult, OutputCallback, Sandbox, SandboxSpec

from .kube import (
    API_VERSION,
    SANDBOX_GROUP,
    SANDBOX_PLURAL,
    ExecRequest,
    KubeApi,
)

if TYPE_CHECKING:
    from .provider import K8sAgentSandboxProvider

CONTAINER_NAME = "workspace"


class K8sAgentSandbox(Sandbox):
    """One live Sandbox custom resource; safe for concurrent ``exec`` calls."""

    def __init__(
        self,
        provider: K8sAgentSandboxProvider,
        sandbox_id: str,
        spec: SandboxSpec,
        *,
        claimed: bool = False,
    ) -> None:
        self._provider = provider
        self.id = sandbox_id
        self.spec = spec
        self.claimed = claimed
        self._destroyed = False

    @property
    def _api(self) -> KubeApi:
        return self._provider.api

    def _check_live(self) -> None:
        if self._destroyed:
            raise SandboxLost(detail={"sandbox_id": self.id, "reason": "destroyed"})

    async def exec(
        self,
        cmd: str | list[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        user: str | None = None,
        timeout_s: float | None = None,
        stdin: bytes | None = None,
        on_output: OutputCallback | None = None,
        max_output_bytes: int = 8 << 20,
    ) -> ExecResult:
        self._check_live()
        del user  # the pod image decides the account; run_as_user is a pod security context
        argv = ("/bin/sh", "-lc", cmd) if isinstance(cmd, str) else tuple(str(a) for a in cmd)
        merged_env = dict(self.spec.env)
        merged_env.update(env or {})
        return await self._api.exec_in_pod(
            ExecRequest(
                namespace=self._provider.namespace,
                pod=self.id,
                container=CONTAINER_NAME,
                argv=argv,
                env=merged_env,
                cwd=cwd,
                stdin=stdin,
                timeout_s=timeout_s,
                on_output=on_output,
                max_output_bytes=max_output_bytes,
            )
        )

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        if read_only:
            raise CapabilityNotSupported("read_only_upload")
        self._check_live()
        _require_absolute(remote)
        await self._api.copy_to_pod(
            namespace=self._provider.namespace,
            pod=self.id,
            container=CONTAINER_NAME,
            source=local,
            destination=remote,
        )

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        self._check_live()
        _require_absolute(remote)
        await self._api.copy_from_pod(
            namespace=self._provider.namespace,
            pod=self.id,
            container=CONTAINER_NAME,
            source=remote,
            destination=local,
            max_bytes=max_bytes,
        )

    async def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        await self._provider.release_sandbox(self)

    async def describe(self) -> dict[str, Any]:
        body = await self._api.get_object(
            group=SANDBOX_GROUP,
            version=API_VERSION,
            plural=SANDBOX_PLURAL,
            namespace=self._provider.namespace,
            name=self.id,
        )
        if body is None:
            return {}
        status = body.get("status", {})
        return {
            "host": "kubernetes",
            "namespace": self._provider.namespace,
            "runtime_class": self._provider.runtime_class,
            "claimed": self.claimed,
            "pod_ip": status.get("podIPs") or status.get("podIP"),
            "conditions": status.get("conditions", []),
        }


def _require_absolute(remote: str) -> None:
    if not PurePosixPath(remote).is_absolute():
        raise TransferError(f"remote path must be absolute, got {remote!r}")
