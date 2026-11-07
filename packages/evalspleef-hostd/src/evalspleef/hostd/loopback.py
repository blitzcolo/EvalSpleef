"""A complete control plane and daemon on loopback, for tests and conformance targets.

``LoopbackHostd`` wires a CA, a registry, hostd-gateway, a daemon registered with a one-time token
and a RemoteProvider holding the control-plane certificate, all over real mTLS gRPC sockets.
``ThreadedLoopbackHostd`` runs the same stack on a private event-loop thread for callers (the
conformance plugin) that drive every case from a fresh loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import socket
import threading
from collections.abc import Mapping
from typing import Any, TypeVar

from evalspleef.spi.sandbox import SandboxProvider

from .clock import Clock
from .daemon import ControlLink, HostDaemon, NoSecrets
from .gateway import HostGateway
from .hosts import HostRegistry
from .remote import RemoteProvider
from .security import CertificateAuthority, ClientCredentials, RegistrationTokens

T = TypeVar("T")

LOOPBACK = "127.0.0.1"


def free_port() -> int:
    """A currently free TCP port; the daemon must know its port before it registers."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


class LoopbackHostd:
    def __init__(
        self,
        *,
        ca: CertificateAuthority,
        registry: HostRegistry,
        tokens: RegistrationTokens,
        gateway: HostGateway,
        daemon: HostDaemon,
        control: ClientCredentials,
        service_port: int,
    ) -> None:
        self.ca = ca
        self.registry = registry
        self.tokens = tokens
        self.gateway = gateway
        self.daemon = daemon
        self.control = control
        self.service_port = service_port
        self._remotes: list[RemoteProvider] = []

    @classmethod
    async def start(
        cls,
        providers: Mapping[str, SandboxProvider],
        *,
        host_id: str = "host-1",
        adapter: str = "mock-sandbox",
        max_concurrency: Mapping[str, int] | None = None,
        daemon_clock: Clock | None = None,
        registry_clock: Clock | None = None,
    ) -> LoopbackHostd:
        ca = CertificateAuthority.create()
        registry = HostRegistry(registry_clock)
        tokens = RegistrationTokens(registry_clock)
        gateway = HostGateway(ca, registry, tokens, clock=registry_clock)
        await gateway.start(LOOPBACK)
        daemon = HostDaemon(host_id, clock=daemon_clock)
        for name, provider in providers.items():
            daemon.add_provider(
                name,
                provider,
                adapter=adapter,
                max_concurrency=(max_concurrency or {}).get(name),
            )
        port = free_port()
        address = f"{LOOPBACK}:{port}"
        link = ControlLink(
            registration_address=f"{LOOPBACK}:{gateway.registration_port}",
            gateway_address=f"{LOOPBACK}:{gateway.gateway_port}",
            ca_pem=ca.cert_pem,
        )
        credentials = await daemon.register(link, tokens.issue(host_id), listen_address=address)
        await daemon.serve(address, credentials)
        return cls(
            ca=ca,
            registry=registry,
            tokens=tokens,
            gateway=gateway,
            daemon=daemon,
            control=gateway.control_credentials(),
            service_port=port,
        )

    def remote(
        self,
        provider: str,
        *,
        credentials: ClientCredentials | None = None,
        use_registry: bool = False,
        **config: Any,
    ) -> RemoteProvider:
        remote = RemoteProvider(
            f"remote-{provider}",
            {"address": f"{LOOPBACK}:{self.service_port}", "provider": provider, **config},
            NoSecrets(),
            credentials=credentials or self.control,
            registry=self.registry if use_registry else None,
            host_id=self.daemon.host_id if use_registry else None,
        )
        self._remotes.append(remote)
        return remote

    async def stop(self) -> None:
        for remote in self._remotes:
            await remote.aclose()
        await self.daemon.close()
        await self.gateway.stop()


class ThreadedLoopbackHostd:
    """A ``LoopbackHostd`` living on its own event-loop thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="loopback-hostd", daemon=True
        )
        self._thread.start()
        self.stack: LoopbackHostd | None = None

    def run(self, coroutine: Any, timeout: float = 60.0) -> Any:
        future: concurrent.futures.Future[Any] = asyncio.run_coroutine_threadsafe(
            coroutine, self._loop
        )
        return future.result(timeout)

    def start(self, providers: Mapping[str, SandboxProvider], **kwargs: Any) -> LoopbackHostd:
        self.stack = self.run(LoopbackHostd.start(providers, **kwargs))
        assert self.stack is not None
        return self.stack

    def stop(self) -> None:
        if self.stack is not None:
            self.run(self.stack.stop())
            self.stack = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)
        self._loop.close()
