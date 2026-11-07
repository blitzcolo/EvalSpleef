"""The host daemon: local sandbox providers exposed to the control plane (TECH-DESIGN 7.5).

The daemon dials out to hostd-gateway to register (one-time token -> client certificate) and to
heartbeat, and serves the ``Hostd`` gRPC service on an mTLS port the control plane's
RemoteProvider calls. It owns what must hold even when disconnected: the per-provider concurrency
cap and the local TTL guard that destroys instances past their lifetime.
"""

# mypy: disable-error-code="import-untyped"
from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import functools
import logging
import os
import platform
import shutil
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, Literal

import grpc
import yaml

from evalspleef.spi.errors import (
    CapabilityNotSupported,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    TransferError,
)
from evalspleef.spi.sandbox import Sandbox, SandboxCapabilities, SandboxProvider, SandboxSpec
from evalspleef.spi.secrets import SecretRef

from . import codec, transfer
from ._pb import hostd_pb2 as pb
from .clock import Clock, SystemClock
from .hosts import HEARTBEAT_INTERVAL_S, HostMode
from .rpcerrors import (
    Handler,
    HostdAuthError,
    StreamHandler,
    stream_guard,
    translate,
    unary_guard,
)
from .security import (
    CONTROL_PLANE_IDENTITY,
    ClientCredentials,
    certificate_not_after,
    generate_key_and_csr,
    needs_rotation,
    peer_common_name,
)
from .stubs import add_hostd_service, gateway_stub, registration_stub
from .wire import CHANNEL_OPTIONS, SERVER_OPTIONS

logger = logging.getLogger(__name__)

HOSTD_VERSION = "0.1.0"
TTL_GUARD_INTERVAL_S = 5.0
OutputStream = Literal["stdout", "stderr"]
CallKind = Literal["create", "sandbox", "control", "admin"]


class NoSecrets:
    """hostd never holds model or vendor secrets (TECH-DESIGN 7.5): every lookup fails."""

    def resolve(self, ref: SecretRef | str) -> str:
        raise KeyError(f"hostd holds no secrets; cannot resolve {ref!r}")


@dataclass(frozen=True)
class ControlLink:
    """Where and how the daemon reaches hostd-gateway."""

    registration_address: str
    gateway_address: str
    ca_pem: bytes
    server_name: str | None = None
    """Overrides the name checked against the gateway certificate, when it differs from the host."""


@dataclass
class _Tracked:
    sandbox: Sandbox
    spec: SandboxSpec
    expires_at: float


@dataclass
class HostedProvider:
    name: str
    adapter: str
    provider: SandboxProvider
    max_concurrency: int | None = None
    sandboxes: dict[str, _Tracked] = field(default_factory=dict)
    reserved: int = 0
    """Creates in flight; counted against the cap before the provider call starts."""

    def capabilities(self) -> SandboxCapabilities:
        """Provider capabilities with the local concurrency cap folded in."""
        caps = self.provider.capabilities()
        caps_limit = caps.max_concurrency
        limits = [limit for limit in (self.max_concurrency, caps_limit) if limit is not None]
        if not limits:
            return caps
        return dataclasses.replace(caps, max_concurrency=min(limits))


def load_adapter(adapter: str) -> type[SandboxProvider]:
    """Find a sandbox adapter by entry-point name; adapter packages are never imported statically."""
    matches = entry_points(group="evalspleef.sandbox_providers", name=adapter)
    for entry_point in matches:
        loaded = entry_point.load()
        if not (isinstance(loaded, type) and issubclass(loaded, SandboxProvider)):
            raise ValueError(f"entry point {adapter!r} is not a SandboxProvider")
        return loaded
    raise ValueError(f"no sandbox adapter named {adapter!r} is installed")


class HostDaemon:
    def __init__(
        self,
        host_id: str,
        *,
        clock: Clock | None = None,
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
        ttl_guard_interval_s: float = TTL_GUARD_INTERVAL_S,
        allowed_clients: Sequence[str] = (CONTROL_PLANE_IDENTITY,),
        work_dir: Path | None = None,
        control_timeout_s: float = 10.0,
    ) -> None:
        self.host_id = host_id
        self.mode: HostMode = "active"
        self._clock: Clock = clock or SystemClock()
        self._heartbeat_interval_s = heartbeat_interval_s
        self._ttl_guard_interval_s = ttl_guard_interval_s
        self._allowed_clients = frozenset(allowed_clients)
        self._work_dir = work_dir
        self.control_timeout_s = control_timeout_s
        """Deadline for heartbeat and report calls; they wait for a reconnect within it."""
        self._hosted: dict[str, HostedProvider] = {}
        self._pending_reclaimed: list[pb.ReclaimedSandbox] = []
        self.connected = True
        self._link: ControlLink | None = None
        self._credentials: ClientCredentials | None = None
        self._cert_generation = 0
        self._served_generation = 0
        self._server: grpc.aio.Server | None = None
        self._gateway_channel: grpc.aio.Channel | None = None

    # -- providers -------------------------------------------------------------------

    def add_provider(
        self,
        name: str,
        provider: SandboxProvider,
        *,
        adapter: str,
        max_concurrency: int | None = None,
    ) -> None:
        self._hosted[name] = HostedProvider(name, adapter, provider, max_concurrency)

    def add_adapter(
        self,
        name: str,
        adapter: str,
        config: Mapping[str, Any],
        *,
        max_concurrency: int | None = None,
    ) -> None:
        """Instantiate a local provider through its entry point."""
        provider_class = load_adapter(adapter)
        self.add_provider(
            name,
            provider_class(name, config, NoSecrets()),
            adapter=adapter,
            max_concurrency=max_concurrency,
        )

    def hosted(self, name: str) -> HostedProvider:
        try:
            return self._hosted[name]
        except KeyError:
            raise CapabilityNotSupported(
                f"this host does not run a provider named {name!r}"
            ) from None

    def _tracked(self, provider: str, sandbox_id: str) -> _Tracked:
        tracked = self.hosted(provider).sandboxes.get(sandbox_id)
        if tracked is None:
            raise SandboxLost(f"sandbox {sandbox_id} is not running on this host")
        return tracked

    # -- admission --------------------------------------------------------------------

    def authorize(self, context: Any) -> None:
        peer = peer_common_name(context)
        if peer is None or peer not in self._allowed_clients:
            raise HostdAuthError(f"client identity {peer!r} is not allowed to call this host")

    def gate(self, kind: CallKind) -> None:
        """Apply drain and maintenance: drain stops new leases, maintenance stops every call."""
        if kind == "admin":
            return
        if self.mode == "maintenance":
            if kind == "sandbox":
                raise SandboxLost(f"host {self.host_id} is in maintenance")
            raise SandboxCreateError(f"host {self.host_id} is in maintenance")
        if self.mode == "draining" and kind == "create":
            raise SandboxCreateError(f"host {self.host_id} is draining and takes no new sandboxes")

    def set_drain(self, enabled: bool) -> HostMode:
        if enabled:
            if self.mode == "active":
                self.mode = "draining"
        elif self.mode == "draining":
            self.mode = "active"
        return self.mode

    def set_maintenance(self, enabled: bool) -> HostMode:
        if enabled:
            self.mode = "maintenance"
        elif self.mode == "maintenance":
            self.mode = "active"
        return self.mode

    # -- sandbox lifecycle ---------------------------------------------------------------

    async def create(
        self, provider_name: str, spec: SandboxSpec, snapshot_id: str | None
    ) -> Sandbox:
        hosted = self.hosted(provider_name)
        cap = hosted.capabilities().max_concurrency
        if cap is not None and len(hosted.sandboxes) + hosted.reserved >= cap:
            raise QuotaExceeded(f"provider {provider_name} is at its local cap of {cap} sandboxes")
        hosted.reserved += 1
        try:
            if snapshot_id is None:
                sandbox = await hosted.provider.create(spec)
            else:
                sandbox = await hosted.provider.create_from_snapshot(snapshot_id, spec)
        finally:
            hosted.reserved -= 1
        hosted.sandboxes[sandbox.id] = _Tracked(
            sandbox, spec, self._clock.now() + spec.max_lifetime_s
        )
        return sandbox

    async def destroy(self, provider_name: str, sandbox_id: str) -> None:
        hosted = self.hosted(provider_name)
        tracked = hosted.sandboxes.get(sandbox_id)
        if tracked is None:
            return  # destroying a missing instance succeeds
        await tracked.sandbox.destroy()
        hosted.sandboxes.pop(sandbox_id, None)

    async def reap(self, provider_name: str, labels: Mapping[str, str], older_than_s: int) -> int:
        hosted = self.hosted(provider_name)
        destroyed = await hosted.provider.reap(labels, older_than_s)
        if destroyed:
            alive = {str(entry["id"]) for entry in await hosted.provider.list({})}
            for sandbox_id in [key for key in hosted.sandboxes if key not in alive]:
                del hosted.sandboxes[sandbox_id]
        return destroyed

    async def ttl_guard_once(self) -> list[pb.ReclaimedSandbox]:
        """Destroy every instance past its lifetime, whether or not the control plane is reachable."""
        now = self._clock.now()
        reclaimed: list[pb.ReclaimedSandbox] = []
        for hosted in self._hosted.values():
            for sandbox_id, tracked in list(hosted.sandboxes.items()):
                if now < tracked.expires_at:
                    continue
                try:
                    await tracked.sandbox.destroy()
                except Exception:
                    # The guard must outlive one failing provider; the next pass retries.
                    logger.exception("ttl guard could not destroy %s/%s", hosted.name, sandbox_id)
                    continue
                del hosted.sandboxes[sandbox_id]
                reclaimed.append(
                    pb.ReclaimedSandbox(
                        provider=hosted.name,
                        sandbox_id=sandbox_id,
                        trial_id=tracked.spec.trial_id,
                        reason="ttl",
                    )
                )
        self._pending_reclaimed.extend(reclaimed)
        return reclaimed

    @property
    def pending_reclaimed(self) -> list[pb.ReclaimedSandbox]:
        return list(self._pending_reclaimed)

    # -- inventory and host facts ---------------------------------------------------------

    def inventory(self) -> pb.HostInventory:
        slots = []
        for hosted in self._hosted.values():
            entry = pb.ProviderSlots(
                provider=hosted.name, used=len(hosted.sandboxes), reserved=hosted.reserved
            )
            cap = hosted.capabilities().max_concurrency
            if cap is not None:
                entry.total = cap
            slots.append(entry)
        return pb.HostInventory(
            host_id=self.host_id,
            mode=self.mode,
            slots=slots,
            sandbox_ids=[
                f"{hosted.name}/{sandbox_id}"
                for hosted in self._hosted.values()
                for sandbox_id in hosted.sandboxes
            ],
            hostd_version=HOSTD_VERSION,
        )

    def host_info(self) -> pb.HostInfo:
        try:
            memory_mb = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1 << 20)
        except (ValueError, OSError, AttributeError):
            memory_mb = 0
        return pb.HostInfo(
            machine=platform.node(),
            os=platform.system().lower(),
            arch=platform.machine(),
            cpu_count=os.cpu_count() or 0,
            memory_mb=memory_mb,
            disk_free_mb=self._disk_free_mb(),
            hostd_version=HOSTD_VERSION,
            providers=[
                pb.ProviderInfo(
                    name=hosted.name,
                    adapter=hosted.adapter,
                    capabilities=codec.capabilities_to_pb(hosted.capabilities()),
                )
                for hosted in self._hosted.values()
            ],
        )

    def _disk_free_mb(self) -> int:
        return shutil.disk_usage(self._work_dir or tempfile.gettempdir()).free // (1 << 20)

    # -- serving the Hostd service ------------------------------------------------------------

    async def serve(self, listen_address: str, credentials: ClientCredentials) -> int:
        """Start the mTLS Hostd service; returns the bound port."""
        self._credentials = credentials
        server = grpc.aio.server(options=SERVER_OPTIONS)
        add_hostd_service(_HostdService(self), server)

        def fetch_configuration() -> Any:
            # Certificate rotation: new connections pick up the renewed certificate.
            if self._served_generation == self._cert_generation or self._credentials is None:
                return None
            self._served_generation = self._cert_generation
            return self._credentials.server_configuration()

        port = server.add_secure_port(
            listen_address,
            grpc.dynamic_ssl_server_credentials(
                credentials.server_configuration(),
                fetch_configuration,
                require_client_authentication=True,
            ),
        )
        await server.start()
        self._server = server
        return int(port)

    async def stop_serving(self, grace: float | None = None) -> None:
        if self._server is not None:
            await self._server.stop(grace)
            self._server = None

    async def close(self) -> None:
        await self.stop_serving()
        if self._gateway_channel is not None:
            await self._gateway_channel.close()
            self._gateway_channel = None

    # -- talking to the control plane ---------------------------------------------------------

    def _channel_options(self) -> list[tuple[str, Any]]:
        options: list[tuple[str, Any]] = list(CHANNEL_OPTIONS)
        if self._link is not None and self._link.server_name is not None:
            options.append(("grpc.ssl_target_name_override", self._link.server_name))
        return options

    async def register(
        self,
        link: ControlLink,
        token: str,
        *,
        listen_address: str,
        advertise_address: str | None = None,
        service_names: Sequence[str] = ("localhost", "127.0.0.1"),
    ) -> ClientCredentials:
        """Exchange the one-time token for a client certificate; the private key stays here."""
        self._link = link
        key_pem, csr_pem = generate_key_and_csr(self.host_id)
        channel = grpc.aio.secure_channel(
            link.registration_address,
            grpc.ssl_channel_credentials(root_certificates=link.ca_pem),
            options=self._channel_options(),
        )
        try:
            response = await registration_stub(channel).Register(
                pb.RegisterRequest(
                    host_id=self.host_id,
                    token=token,
                    csr_pem=csr_pem,
                    service_address=advertise_address or listen_address,
                    service_names=list(service_names),
                    info=self.host_info(),
                ),
                timeout=30,
            )
        except grpc.aio.AioRpcError as exc:
            raise translate(exc, fallback=SandboxCreateError) from exc
        finally:
            await channel.close()
        self._credentials = ClientCredentials(
            ca_pem=bytes(response.ca_pem), cert_pem=bytes(response.cert_pem), key_pem=key_pem
        )
        return self._credentials

    def use_credentials(self, link: ControlLink, credentials: ClientCredentials) -> None:
        """Adopt previously issued credentials (restart without a fresh token)."""
        self._link = link
        self._credentials = credentials

    def _gateway_stub(self) -> Any:
        if self._link is None or self._credentials is None:
            raise HostdAuthError("daemon is not registered with a control plane")
        if self._gateway_channel is None:
            self._gateway_channel = grpc.aio.secure_channel(
                self._link.gateway_address,
                self._credentials.channel_credentials(),
                options=self._channel_options(),
            )
        return gateway_stub(self._gateway_channel)

    async def heartbeat_once(self) -> bool:
        """One heartbeat; after any successful one, reclaimed instances are reported."""
        request = pb.HeartbeatRequest(
            host_id=self.host_id,
            inventory=self.inventory(),
            disk_free_mb=self._disk_free_mb(),
            load_1m=os.getloadavg()[0] if hasattr(os, "getloadavg") else 0.0,
        )
        try:
            await self._gateway_stub().Heartbeat(
                request, timeout=self.control_timeout_s, wait_for_ready=True
            )
        except grpc.aio.AioRpcError as exc:
            if self.connected:
                logger.warning("heartbeat to the control plane failed: %s", exc.code().name)
            self.connected = False
            return False
        self.connected = True
        await self._flush_reclaimed()
        return True

    async def _flush_reclaimed(self) -> None:
        if not self._pending_reclaimed:
            return
        batch = list(self._pending_reclaimed)
        try:
            await self._gateway_stub().ReportReclaimed(
                pb.ReclaimedReport(host_id=self.host_id, reclaimed=batch),
                timeout=self.control_timeout_s,
                wait_for_ready=True,
            )
        except grpc.aio.AioRpcError as exc:
            logger.warning("could not report reclaimed sandboxes: %s", exc.code().name)
            return
        del self._pending_reclaimed[: len(batch)]

    async def rotate_if_due(self) -> bool:
        """Renew the certificate through the gateway once it is within the rotation margin."""
        credentials = self._credentials
        if credentials is None or not needs_rotation(credentials.not_after, self._clock.now()):
            return False
        key_pem, csr_pem = generate_key_and_csr(self.host_id)
        try:
            response = await self._gateway_stub().Renew(
                pb.RenewRequest(host_id=self.host_id, csr_pem=csr_pem), timeout=30
            )
        except grpc.aio.AioRpcError as exc:
            raise translate(exc, fallback=SandboxCreateError) from exc
        self._credentials = ClientCredentials(
            ca_pem=bytes(response.ca_pem), cert_pem=bytes(response.cert_pem), key_pem=key_pem
        )
        self._cert_generation += 1
        if self._gateway_channel is not None:
            await self._gateway_channel.close()
            self._gateway_channel = None
        return True

    # -- background loops -----------------------------------------------------------------------

    async def heartbeat_loop(self) -> None:
        while True:
            await self.heartbeat_once()
            try:
                await self.rotate_if_due()
            except (SandboxCreateError, HostdAuthError):
                logger.exception("certificate rotation failed; retrying at the next heartbeat")
            await self._clock.sleep(self._heartbeat_interval_s)

    async def ttl_guard_loop(self) -> None:
        while True:
            await self.ttl_guard_once()
            await self._clock.sleep(self._ttl_guard_interval_s)

    # -- scratch space ----------------------------------------------------------------------------

    def scratch_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix="hostd-", dir=self._work_dir))


def _service_call(kind: CallKind) -> Callable[[Handler], Handler]:
    def decorate(handler: Handler) -> Handler:
        @functools.wraps(handler)
        async def wrapper(self: _HostdService, request: Any, context: Any) -> Any:
            self.daemon.authorize(context)
            self.daemon.gate(kind)
            return await handler(self, request, context)

        return unary_guard(wrapper)

    return decorate


def _service_stream(kind: CallKind) -> Callable[[StreamHandler], StreamHandler]:
    def decorate(handler: StreamHandler) -> StreamHandler:
        @functools.wraps(handler)
        async def wrapper(self: _HostdService, request: Any, context: Any) -> AsyncIterator[Any]:
            self.daemon.authorize(context)
            self.daemon.gate(kind)
            async for item in handler(self, request, context):
                yield item

        return stream_guard(wrapper)

    return decorate


class _HostdService:
    """The gRPC face of ``HostDaemon``; method names are the RPC names."""

    def __init__(self, daemon: HostDaemon) -> None:
        self.daemon = daemon

    @_service_call("control")
    async def Capabilities(self, request: pb.ProviderRef, context: Any) -> Any:
        return codec.capabilities_to_pb(self.daemon.hosted(request.provider).capabilities())

    @_service_call("control")
    async def GetIsolationProfile(self, request: pb.ProviderRef, context: Any) -> Any:
        return codec.profile_to_pb(
            self.daemon.hosted(request.provider).provider.isolation_profile()
        )

    @_service_call("control")
    async def Preflight(self, request: pb.ProviderRef, context: Any) -> Any:
        report = await self.daemon.hosted(request.provider).provider.preflight()
        return pb.JsonDocument(json=codec.to_json(report))

    @_service_call("create")
    async def Create(self, request: pb.CreateRequest, context: Any) -> Any:
        snapshot_id = request.snapshot_id if request.HasField("snapshot_id") else None
        sandbox = await self.daemon.create(
            request.provider, codec.spec_from_pb(request.spec), snapshot_id
        )
        return pb.CreateResponse(sandbox_id=sandbox.id, spec=request.spec)

    @_service_stream("sandbox")
    async def Exec(self, request: pb.ExecRequest, context: Any) -> AsyncIterator[pb.ExecEvent]:
        sandbox = self.daemon._tracked(request.provider, request.sandbox_id).sandbox
        queue: asyncio.Queue[tuple[OutputStream, bytes] | None] = asyncio.Queue()

        def on_output(stream: OutputStream, data: bytes) -> None:
            queue.put_nowait((stream, data))

        command: str | list[str] = (
            request.shell_command if request.HasField("shell_command") else list(request.argv)
        )
        task = asyncio.create_task(
            sandbox.exec(
                command,
                cwd=request.cwd if request.HasField("cwd") else None,
                env=dict(request.env) or None,
                user=request.user if request.HasField("user") else None,
                timeout_s=request.timeout_s if request.HasField("timeout_s") else None,
                stdin=bytes(request.stdin) if request.HasField("stdin") else None,
                on_output=on_output,
                max_output_bytes=request.max_output_bytes or (8 << 20),
            )
        )
        task.add_done_callback(lambda _: queue.put_nowait(None))
        try:
            while (item := await queue.get()) is not None:
                yield pb.ExecEvent(live=pb.OutputChunk(stream=item[0], data=item[1]))
            result = task.result()
        finally:
            if not task.done():
                task.cancel()
        for stream, payload in (("stdout", result.stdout), ("stderr", result.stderr)):
            for offset in range(0, len(payload), transfer.CHUNK_SIZE):
                piece = payload[offset : offset + transfer.CHUNK_SIZE]
                yield pb.ExecEvent(final_chunk=pb.OutputChunk(stream=stream, data=piece))
        yield pb.ExecEvent(
            summary=pb.ExecSummary(
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                duration_s=result.duration_s,
                truncated=result.truncated,
            )
        )

    @_service_call("sandbox")
    async def Upload(self, request_iterator: AsyncIterator[pb.UploadMessage], context: Any) -> Any:
        header: pb.TransferHeader | None = None
        scratch = self.daemon.scratch_dir()
        try:
            archive = scratch / "upload.tar"
            total = 0
            finished = False
            with archive.open("wb") as sink:
                receiver = transfer.ChunkReceiver(sink)
                async for message in request_iterator:
                    part = message.WhichOneof("part")
                    if part == "header":
                        header = message.header
                        self.daemon._tracked(header.provider, header.sandbox_id)
                    elif header is None:
                        raise TransferError("upload stream must start with a header")
                    elif part == "chunk":
                        receiver.accept(message.chunk)
                    elif part == "trailer":
                        total = receiver.finish(message.trailer)
                        finished = True
            if header is None or not finished:
                raise TransferError("upload ended before its trailer: transfer incomplete")
            payload = await asyncio.to_thread(
                transfer.unpack, archive, scratch / "unpacked", strict=False
            )
            sandbox = self.daemon._tracked(header.provider, header.sandbox_id).sandbox
            await sandbox.upload(payload, header.remote)
            return pb.UploadAck(total_bytes=total)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @_service_stream("sandbox")
    async def Download(
        self, request: pb.DownloadRequest, context: Any
    ) -> AsyncIterator[pb.DownloadMessage]:
        sandbox = self.daemon._tracked(request.provider, request.sandbox_id).sandbox
        scratch = self.daemon.scratch_dir()
        try:
            fetched = scratch / "fetched"
            await sandbox.download(
                request.remote,
                fetched,
                max_bytes=request.max_bytes if request.HasField("max_bytes") else None,
            )
            archive = scratch / "download.tar"
            await asyncio.to_thread(transfer.pack, fetched, archive)
            reader = transfer.ChunkReader(archive)
            for chunk in reader:
                yield pb.DownloadMessage(chunk=chunk)
            yield pb.DownloadMessage(trailer=reader.trailer)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    @_service_call("sandbox")
    async def Destroy(self, request: pb.SandboxRef, context: Any) -> Any:
        await self.daemon.destroy(request.provider, request.sandbox_id)
        return pb.Empty()

    @_service_call("control")
    async def List(self, request: pb.LabelQuery, context: Any) -> Any:
        entries = await self.daemon.hosted(request.provider).provider.list(dict(request.labels))
        return pb.ListResponse(entries=[pb.JsonDocument(json=codec.to_json(e)) for e in entries])

    @_service_call("control")
    async def Reap(self, request: pb.ReapRequest, context: Any) -> Any:
        destroyed = await self.daemon.reap(
            request.provider, dict(request.labels), request.older_than_s
        )
        return pb.ReapResponse(destroyed=destroyed)

    @_service_call("sandbox")
    async def Snapshot(self, request: pb.SnapshotRequest, context: Any) -> Any:
        sandbox = self.daemon._tracked(request.provider, request.sandbox_id).sandbox
        return pb.SnapshotResponse(snapshot_id=await sandbox.snapshot(request.name))

    @_service_call("sandbox")
    async def SetNetwork(self, request: pb.SetNetworkRequest, context: Any) -> Any:
        sandbox = self.daemon._tracked(request.provider, request.sandbox_id).sandbox
        await sandbox.set_network(codec.network_from_pb(request.policy))
        return pb.Empty()

    @_service_call("sandbox")
    async def GuiEndpoint(self, request: pb.SandboxRef, context: Any) -> Any:
        sandbox = self.daemon._tracked(request.provider, request.sandbox_id).sandbox
        return pb.EndpointResponse(endpoint=await sandbox.gui_endpoint())

    @_service_call("sandbox")
    async def ExposePort(self, request: pb.ExposePortRequest, context: Any) -> Any:
        sandbox = self.daemon._tracked(request.provider, request.sandbox_id).sandbox
        return pb.EndpointResponse(endpoint=await sandbox.expose_port(request.port))

    @_service_call("sandbox")
    async def Describe(self, request: pb.SandboxRef, context: Any) -> Any:
        sandbox = self.daemon._tracked(request.provider, request.sandbox_id).sandbox
        return pb.JsonDocument(json=codec.to_json(await sandbox.describe()))

    @_service_call("admin")
    async def Inventory(self, request: pb.Empty, context: Any) -> Any:
        return self.daemon.inventory()

    @_service_call("admin")
    async def Drain(self, request: pb.ModeRequest, context: Any) -> Any:
        return pb.HostMode(mode=self.daemon.set_drain(request.enabled))

    @_service_call("admin")
    async def SetMaintenance(self, request: pb.ModeRequest, context: Any) -> Any:
        return pb.HostMode(mode=self.daemon.set_maintenance(request.enabled))


# -- configuration and entry point ---------------------------------------------------------------


@dataclass(frozen=True)
class DaemonConfig:
    host_id: str
    listen: str
    advertise: str
    service_names: tuple[str, ...]
    link: ControlLink
    credentials_dir: Path
    registration_token_env: str
    heartbeat_interval_s: float
    ttl_guard_interval_s: float
    allowed_clients: tuple[str, ...]
    providers: tuple[Mapping[str, Any], ...]

    @classmethod
    def load(cls, path: Path) -> DaemonConfig:
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: expected a mapping at the top level")
        control = raw["control"]
        base = path.parent
        listen = str(raw["listen"])
        return cls(
            host_id=str(raw["host_id"]),
            listen=listen,
            advertise=str(raw.get("advertise", listen)),
            service_names=tuple(raw.get("service_names", ["localhost", "127.0.0.1"])),
            link=ControlLink(
                registration_address=str(control["registration_address"]),
                gateway_address=str(control["gateway_address"]),
                ca_pem=(base / control["ca_file"]).read_bytes(),
                server_name=control.get("server_name"),
            ),
            credentials_dir=base / raw.get("credentials_dir", "credentials"),
            registration_token_env=str(
                raw.get("registration_token_env", "HOSTD_REGISTRATION_TOKEN")
            ),
            heartbeat_interval_s=float(raw.get("heartbeat_interval_s", HEARTBEAT_INTERVAL_S)),
            ttl_guard_interval_s=float(raw.get("ttl_guard_interval_s", TTL_GUARD_INTERVAL_S)),
            allowed_clients=tuple(raw.get("allowed_clients", [CONTROL_PLANE_IDENTITY])),
            providers=tuple(raw["providers"]),
        )


def build_daemon(config: DaemonConfig, clock: Clock | None = None) -> HostDaemon:
    daemon = HostDaemon(
        config.host_id,
        clock=clock,
        heartbeat_interval_s=config.heartbeat_interval_s,
        ttl_guard_interval_s=config.ttl_guard_interval_s,
        allowed_clients=config.allowed_clients,
    )
    for entry in config.providers:
        daemon.add_adapter(
            str(entry["name"]),
            str(entry["adapter"]),
            entry.get("config", {}),
            max_concurrency=entry.get("max_concurrency"),
        )
    return daemon


def _load_credentials(directory: Path, link: ControlLink) -> ClientCredentials | None:
    key, cert = directory / "key.pem", directory / "cert.pem"
    if not (key.is_file() and cert.is_file()):
        return None
    return ClientCredentials(
        ca_pem=link.ca_pem, cert_pem=cert.read_bytes(), key_pem=key.read_bytes()
    )


def _store_credentials(directory: Path, credentials: ClientCredentials) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, data in (("key.pem", credentials.key_pem), ("cert.pem", credentials.cert_pem)):
        target = directory / name
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)


async def run_daemon(config: DaemonConfig) -> None:
    """Register (or reuse stored credentials), serve, heartbeat and guard TTLs until signalled."""
    daemon = build_daemon(config)
    credentials = _load_credentials(config.credentials_dir, config.link)
    if (
        credentials is not None
        and certificate_not_after(credentials.cert_pem) > daemon._clock.now()
    ):
        daemon.use_credentials(config.link, credentials)
    else:
        token = os.environ.get(config.registration_token_env)
        if not token:
            raise SystemExit(
                f"no stored credentials and {config.registration_token_env} is not set"
            )
        credentials = await daemon.register(
            config.link,
            token,
            listen_address=config.listen,
            advertise_address=config.advertise,
            service_names=config.service_names,
        )
        _store_credentials(config.credentials_dir, credentials)
    await daemon.serve(config.listen, credentials)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stop.set)
    tasks = [
        asyncio.create_task(daemon.heartbeat_loop()),
        asyncio.create_task(daemon.ttl_guard_loop()),
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await daemon.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] in ("-h", "--help"):
        print("usage: spleef-hostd CONFIG.yaml", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run_daemon(DaemonConfig.load(Path(args[0]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
