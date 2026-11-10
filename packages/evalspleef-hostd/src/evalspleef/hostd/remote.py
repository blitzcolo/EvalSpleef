"""RemoteProvider: the SPI on the control-plane side, forwarded to a hostd over mTLS gRPC.

Calls run on a private I/O thread with its own event loop, so one provider object works from any
caller loop (and from synchronous code, which ``capabilities()`` and ``isolation_profile()``
need because the SPI declares them synchronous). Output callbacks are marshalled back onto the
caller's loop in order, before ``exec`` returns.
"""

# mypy: disable-error-code="import-untyped"
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import tempfile
import threading
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from pathlib import Path
from typing import Any, TypeVar, cast

import grpc

from evalspleef.spi.errors import (
    SandboxCreateError,
    SandboxLost,
    SpleefError,
    TransferError,
)
from evalspleef.spi.isolation import IsolationProfile
from evalspleef.spi.sandbox import (
    ExecResult,
    NetworkPolicy,
    OutputCallback,
    Sandbox,
    SandboxCapabilities,
    SandboxProvider,
    SandboxSpec,
)
from evalspleef.spi.secrets import SecretResolver

from . import codec, tracing, transfer
from ._pb import hostd_pb2 as pb
from .hosts import HostRegistry
from .rpcerrors import translate
from .security import ClientCredentials
from .stubs import hostd_stub
from .wire import CHANNEL_OPTIONS

T = TypeVar("T")

DEFAULT_RPC_TIMEOUT_S = 60.0
DEFAULT_CREATE_TIMEOUT_S = 900.0
DEFAULT_TRANSFER_TIMEOUT_S = 1800.0


class _IoLoop:
    """A private event loop on a daemon thread, started on first use."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="hostd-remote-io", daemon=True
                )
                thread.start()
                self._loop, self._thread = loop, thread
            return self._loop

    def submit(self, coroutine: Coroutine[Any, Any, T]) -> concurrent.futures.Future[T]:
        return asyncio.run_coroutine_threadsafe(coroutine, self._ensure())

    def stop(self) -> None:
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = None
        if loop is not None and thread is not None:
            loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=10)
            loop.close()


class RemoteSandbox(Sandbox):
    def __init__(self, provider: RemoteProvider, sandbox_id: str, spec: SandboxSpec) -> None:
        self.id = sandbox_id
        self.spec = spec
        self._provider = provider

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
        request = pb.ExecRequest(
            provider=self._provider.target,
            sandbox_id=self.id,
            env=dict(env or {}),
            max_output_bytes=max_output_bytes,
        )
        if isinstance(cmd, str):
            request.shell_command = cmd
        else:
            request.argv.extend(str(part) for part in cmd)
        if cwd is not None:
            request.cwd = cwd
        if user is not None:
            request.user = user
        if timeout_s is not None:
            request.timeout_s = timeout_s
        if stdin is not None:
            request.stdin = stdin
        caller = asyncio.get_running_loop()

        async def stream(stub: Any) -> ExecResult:
            final = {"stdout": bytearray(), "stderr": bytearray()}
            summary: pb.ExecSummary | None = None
            async for event in stub.Exec(request):
                kind = event.WhichOneof("event")
                if kind == "live":
                    if on_output is not None:
                        caller.call_soon_threadsafe(
                            on_output,
                            cast(Any, event.live.stream),
                            bytes(event.live.data),
                        )
                elif kind == "final_chunk":
                    final[event.final_chunk.stream] += event.final_chunk.data
                elif kind == "summary":
                    summary = event.summary
            if summary is None:
                raise SandboxLost(f"exec stream for sandbox {self.id} ended without a result")
            # The RPC stream is done, but the output callbacks scheduled with
            # ``call_soon_threadsafe`` may still be sitting in the caller's loop
            # queue: yield once so every chunk is delivered before exec returns.
            await asyncio.sleep(0)
            return ExecResult(
                exit_code=summary.exit_code,
                stdout=bytes(final["stdout"]),
                stderr=bytes(final["stderr"]),
                timed_out=summary.timed_out,
                duration_s=summary.duration_s,
                truncated=summary.truncated,
            )

        return await self._provider.call(stream, fallback=SandboxLost, bound=True)

    async def upload(self, local: Path, remote: str, *, read_only: bool = False) -> None:
        source = Path(local)
        if not await asyncio.to_thread(os.path.lexists, source):
            raise TransferError(f"local path does not exist: {source}")

        async def send(stub: Any) -> None:
            with tempfile.TemporaryDirectory(prefix="hostd-up-") as scratch:
                archive = Path(scratch) / "upload.tar"
                await asyncio.to_thread(transfer.pack, source, archive)
                reader = transfer.ChunkReader(archive)

                async def messages() -> Any:
                    yield pb.UploadMessage(
                        header=pb.TransferHeader(
                            provider=self._provider.target,
                            sandbox_id=self.id,
                            remote=remote,
                            read_only=read_only,
                        )
                    )
                    for chunk in reader:
                        yield pb.UploadMessage(chunk=chunk)
                    yield pb.UploadMessage(trailer=reader.trailer)

                await stub.Upload(messages(), timeout=self._provider.transfer_timeout_s)

        await self._provider.call(send, fallback=SandboxLost, bound=True)

    async def download(self, remote: str, local: Path, *, max_bytes: int | None = None) -> None:
        request = pb.DownloadRequest(
            provider=self._provider.target, sandbox_id=self.id, remote=remote
        )
        if max_bytes is not None:
            request.max_bytes = max_bytes
        destination = Path(local)

        async def receive(stub: Any) -> None:
            with tempfile.TemporaryDirectory(prefix="hostd-down-") as scratch:
                archive = Path(scratch) / "download.tar"
                finished = False
                with archive.open("wb") as sink:
                    receiver = transfer.ChunkReceiver(sink)
                    call = stub.Download(request, timeout=self._provider.transfer_timeout_s)
                    async for message in call:
                        part = message.WhichOneof("part")
                        if part == "chunk":
                            receiver.accept(message.chunk)
                        elif part == "trailer":
                            receiver.finish(message.trailer)
                            finished = True
                if not finished:
                    raise TransferError("download ended before its trailer: transfer incomplete")
                payload = await asyncio.to_thread(
                    transfer.unpack, archive, Path(scratch) / "unpacked", strict=True
                )
                await asyncio.to_thread(transfer.replace_path, payload, destination)

        await self._provider.call(receive, fallback=SandboxLost, bound=True)

    async def destroy(self) -> None:
        ref = pb.SandboxRef(provider=self._provider.target, sandbox_id=self.id)
        await self._provider.call(
            lambda stub: stub.Destroy(ref, timeout=self._provider.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )

    async def snapshot(self, name: str) -> str:
        request = pb.SnapshotRequest(provider=self._provider.target, sandbox_id=self.id, name=name)
        response = await self._provider.call(
            lambda stub: stub.Snapshot(request, timeout=self._provider.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )
        return cast(str, response.snapshot_id)

    async def set_network(self, policy: NetworkPolicy) -> None:
        request = pb.SetNetworkRequest(
            provider=self._provider.target,
            sandbox_id=self.id,
            policy=codec.network_to_pb(policy),
        )
        await self._provider.call(
            lambda stub: stub.SetNetwork(request, timeout=self._provider.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )

    async def gui_endpoint(self) -> str:
        ref = pb.SandboxRef(provider=self._provider.target, sandbox_id=self.id)
        response = await self._provider.call(
            lambda stub: stub.GuiEndpoint(ref, timeout=self._provider.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )
        return cast(str, response.endpoint)

    async def expose_port(self, port: int) -> str:
        request = pb.ExposePortRequest(
            provider=self._provider.target, sandbox_id=self.id, port=port
        )
        response = await self._provider.call(
            lambda stub: stub.ExposePort(request, timeout=self._provider.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )
        return cast(str, response.endpoint)

    async def describe(self) -> dict[str, Any]:
        ref = pb.SandboxRef(provider=self._provider.target, sandbox_id=self.id)
        response = await self._provider.call(
            lambda stub: stub.Describe(ref, timeout=self._provider.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )
        return cast(dict[str, Any], codec.from_json(response.json))


class RemoteProvider(SandboxProvider):
    """Forwards every SPI call to one provider hosted by a hostd.

    Config: ``address`` (host:port of the Hostd service), ``provider`` (name of the hosted
    instance), ``ca_file`` / ``cert_file`` / ``key_file`` (control-plane identity, unless
    ``credentials`` is passed), optional ``server_name``, ``rpc_timeout_s``, ``create_timeout_s``.
    With a ``registry`` and ``host_id`` the provider also refuses calls to hosts the registry
    reports as lost (existing sandboxes: SandboxLost) or not active (new leases).
    """

    name = "remote"

    def __init__(
        self,
        instance: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        credentials: ClientCredentials | None = None,
        registry: HostRegistry | None = None,
        host_id: str | None = None,
    ) -> None:
        super().__init__(instance, config, secrets)
        self._address = str(config["address"])
        self.target = str(config["provider"])
        self._server_name = config.get("server_name")
        self.rpc_timeout_s = float(config.get("rpc_timeout_s", DEFAULT_RPC_TIMEOUT_S))
        self.create_timeout_s = float(config.get("create_timeout_s", DEFAULT_CREATE_TIMEOUT_S))
        self.transfer_timeout_s = float(
            config.get("transfer_timeout_s", DEFAULT_TRANSFER_TIMEOUT_S)
        )
        if credentials is None:
            credentials = ClientCredentials(
                ca_pem=Path(config["ca_file"]).read_bytes(),
                cert_pem=Path(config["cert_file"]).read_bytes(),
                key_pem=Path(config["key_file"]).read_bytes(),
            )
        self._credentials = credentials
        self._registry = registry
        self._host_id = host_id
        self._io = _IoLoop()
        self._channel: grpc.aio.Channel | None = None
        self._stub: Any = None
        self._capabilities: SandboxCapabilities | None = None
        self._profile: IsolationProfile | None = None

    # -- plumbing ------------------------------------------------------------------------------

    def _stub_for_io_loop(self) -> Any:
        if self._stub is None:
            options: list[tuple[str, Any]] = list(CHANNEL_OPTIONS)
            if self._server_name is not None:
                options.append(("grpc.ssl_target_name_override", str(self._server_name)))
            self._channel = grpc.aio.secure_channel(
                self._address,
                self._credentials.channel_credentials(),
                options=options,
                interceptors=tracing.channel_interceptors(),
            )
            self._stub = hostd_stub(self._channel)
        return self._stub

    def _gate(self, *, bound: bool) -> None:
        if self._registry is None or self._host_id is None:
            return
        if bound:
            self._registry.require_reachable(self._host_id)
        else:
            self._registry.require_active(self._host_id)

    def _invocation(
        self, operation: Callable[[Any], Awaitable[T]], fallback: type[SpleefError]
    ) -> Coroutine[Any, Any, T]:
        async def run() -> T:
            stub = self._stub_for_io_loop()
            try:
                return await operation(stub)
            except grpc.aio.AioRpcError as exc:
                raise translate(exc, fallback=fallback) from exc

        return run()

    async def call(
        self,
        operation: Callable[[Any], Awaitable[T]],
        *,
        fallback: type[SpleefError],
        bound: bool = False,
        lease: bool = False,
    ) -> T:
        """Run ``operation(stub)`` on the I/O loop; broken connections raise ``fallback``.

        ``bound`` marks calls on a live sandbox (a lost host fails them); ``lease`` marks calls
        that create one (an inactive host refuses them).
        """
        self._gate(bound=not lease)
        return await asyncio.wrap_future(self._io.submit(self._invocation(operation, fallback)))

    def _call_sync(self, operation: Callable[[Any], Awaitable[T]]) -> T:
        self._gate(bound=True)
        future = self._io.submit(self._invocation(operation, SandboxLost))
        return future.result(timeout=self.rpc_timeout_s + 5)

    async def aclose(self) -> None:
        """Close the channel and stop the I/O thread."""
        if self._channel is not None:
            channel = self._channel
            self._channel = self._stub = None
            await asyncio.wrap_future(self._io.submit(channel.close()))
        self._io.stop()

    # -- SandboxProvider ----------------------------------------------------------------------------

    def capabilities(self) -> SandboxCapabilities:
        """Fetched from the host on first use and cached; ``preflight`` refreshes the cache.

        The SPI declares this synchronous, so the first call blocks the calling thread for one
        round trip; await ``preflight()`` first when calling from an event loop.
        """
        if self._capabilities is None:
            ref = pb.ProviderRef(provider=self.target)
            message = self._call_sync(
                lambda stub: stub.Capabilities(ref, timeout=self.rpc_timeout_s)
            )
            self._capabilities = codec.capabilities_from_pb(message)
        return self._capabilities

    def isolation_profile(self) -> IsolationProfile:
        if self._profile is None:
            ref = pb.ProviderRef(provider=self.target)
            message = self._call_sync(
                lambda stub: stub.GetIsolationProfile(ref, timeout=self.rpc_timeout_s)
            )
            self._profile = codec.profile_from_pb(message)
        return self._profile

    async def preflight(self) -> dict[str, Any]:
        ref = pb.ProviderRef(provider=self.target)

        async def refresh(stub: Any) -> tuple[Any, Any, Any]:
            return (
                await stub.Preflight(ref, timeout=self.rpc_timeout_s),
                await stub.Capabilities(ref, timeout=self.rpc_timeout_s),
                await stub.GetIsolationProfile(ref, timeout=self.rpc_timeout_s),
            )

        report, caps, profile = await self.call(refresh, fallback=SandboxLost, bound=True)
        self._capabilities = codec.capabilities_from_pb(caps)
        self._profile = codec.profile_from_pb(profile)
        return cast(dict[str, Any], codec.from_json(report.json))

    async def create(self, spec: SandboxSpec) -> Sandbox:
        request = pb.CreateRequest(provider=self.target, spec=codec.spec_to_pb(spec))
        response = await self.call(
            lambda stub: stub.Create(request, timeout=self.create_timeout_s),
            fallback=SandboxCreateError,
            lease=True,
        )
        return RemoteSandbox(self, response.sandbox_id, spec)

    async def create_from_snapshot(self, snapshot_id: str, spec: SandboxSpec) -> Sandbox:
        request = pb.CreateRequest(
            provider=self.target, spec=codec.spec_to_pb(spec), snapshot_id=snapshot_id
        )
        response = await self.call(
            lambda stub: stub.Create(request, timeout=self.create_timeout_s),
            fallback=SandboxCreateError,
            lease=True,
        )
        return RemoteSandbox(self, response.sandbox_id, spec)

    async def list(self, labels: Mapping[str, str]) -> list[dict[str, Any]]:
        request = pb.LabelQuery(provider=self.target, labels=dict(labels))
        response = await self.call(
            lambda stub: stub.List(request, timeout=self.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )
        return [cast(dict[str, Any], codec.from_json(entry.json)) for entry in response.entries]

    async def reap(self, labels: Mapping[str, str], older_than_s: int) -> int:
        request = pb.ReapRequest(
            provider=self.target, labels=dict(labels), older_than_s=older_than_s
        )
        response = await self.call(
            lambda stub: stub.Reap(request, timeout=self.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )
        return int(response.destroyed)

    # -- host control (not part of the SPI) ----------------------------------------------------------

    async def inventory(self) -> pb.HostInventory:
        return cast(
            pb.HostInventory,
            await self.call(
                lambda stub: stub.Inventory(pb.Empty(), timeout=self.rpc_timeout_s),
                fallback=SandboxLost,
                bound=True,
            ),
        )

    async def drain(self, enabled: bool = True) -> str:
        response = await self.call(
            lambda stub: stub.Drain(pb.ModeRequest(enabled=enabled), timeout=self.rpc_timeout_s),
            fallback=SandboxLost,
            bound=True,
        )
        return cast(str, response.mode)

    async def set_maintenance(self, enabled: bool = True) -> str:
        response = await self.call(
            lambda stub: stub.SetMaintenance(
                pb.ModeRequest(enabled=enabled), timeout=self.rpc_timeout_s
            ),
            fallback=SandboxLost,
            bound=True,
        )
        return cast(str, response.mode)
