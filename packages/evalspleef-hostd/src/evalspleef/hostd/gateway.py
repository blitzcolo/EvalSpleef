"""hostd-gateway: the control-plane side that daemons dial out to.

Two listeners with different trust: registration (TLS with server authentication only, the caller
presents a one-time token instead of a certificate) and the mTLS gateway (heartbeat, certificate
renewal, reclaimed-sandbox reports) that requires a certificate issued by our CA.
"""

from __future__ import annotations

# mypy: disable-error-code="import-untyped"
import asyncio
from collections.abc import Sequence
from typing import Any

import grpc
from google.protobuf import json_format

from ._pb import hostd_pb2 as pb
from .clock import Clock, SystemClock
from .hosts import HostMode, HostRegistry, ReclaimedRecord
from .rpcerrors import HostdAuthError, unary_guard
from .security import (
    CertificateAuthority,
    ClientCredentials,
    RegistrationTokens,
    certificate_not_after,
    peer_common_name,
)
from .stubs import add_gateway_service, add_registration_service
from .wire import SERVER_OPTIONS


def _require_identity(context: Any, host_id: str) -> None:
    peer = peer_common_name(context)
    if peer != host_id:
        raise HostdAuthError(f"certificate identity {peer!r} may not act for host {host_id!r}")


class _RegistrationService:
    def __init__(self, gateway: HostGateway) -> None:
        self._gateway = gateway

    @unary_guard
    async def Register(self, request: pb.RegisterRequest, context: Any) -> pb.RegisterResponse:
        gateway = self._gateway
        gateway.tokens.redeem(request.token, request.host_id)
        cert_pem = gateway.ca.sign_csr(
            request.csr_pem, common_name=request.host_id, names=list(request.service_names)
        )
        slots: dict[str, int | None] = {
            provider.name: (
                provider.capabilities.max_concurrency
                if provider.capabilities.HasField("max_concurrency")
                else None
            )
            for provider in request.info.providers
        }
        gateway.registry.register(
            request.host_id,
            request.service_address,
            info=json_format.MessageToDict(request.info),
            slots=slots,
            hostd_version=request.info.hostd_version,
        )
        return pb.RegisterResponse(
            cert_pem=cert_pem, ca_pem=gateway.ca.cert_pem, not_after=certificate_not_after(cert_pem)
        )


class _GatewayService:
    def __init__(self, gateway: HostGateway) -> None:
        self._gateway = gateway

    @unary_guard
    async def Heartbeat(self, request: pb.HeartbeatRequest, context: Any) -> pb.HeartbeatResponse:
        _require_identity(context, request.host_id)
        inventory = request.inventory
        self._gateway.registry.heartbeat(
            request.host_id,
            mode=_mode(inventory.mode),
            used={slots.provider: slots.used for slots in inventory.slots},
            totals={
                slots.provider: slots.total if slots.HasField("total") else None
                for slots in inventory.slots
            },
            disk_free_mb=request.disk_free_mb,
            load_1m=request.load_1m,
        )
        return pb.HeartbeatResponse(accepted=True)

    @unary_guard
    async def Renew(self, request: pb.RenewRequest, context: Any) -> pb.RegisterResponse:
        _require_identity(context, request.host_id)
        record = self._gateway.registry.get(request.host_id)
        names = [record.service_address.rsplit(":", 1)[0]]
        cert_pem = self._gateway.ca.sign_csr(
            request.csr_pem, common_name=request.host_id, names=names
        )
        return pb.RegisterResponse(
            cert_pem=cert_pem,
            ca_pem=self._gateway.ca.cert_pem,
            not_after=certificate_not_after(cert_pem),
        )

    @unary_guard
    async def ReportReclaimed(self, request: pb.ReclaimedReport, context: Any) -> pb.Empty:
        _require_identity(context, request.host_id)
        self._gateway.registry.record_reclaimed(
            request.host_id,
            [
                ReclaimedRecord(item.provider, item.sandbox_id, item.trial_id, item.reason)
                for item in request.reclaimed
            ],
        )
        return pb.Empty()


_MODES: dict[str, HostMode] = {
    "active": "active",
    "draining": "draining",
    "maintenance": "maintenance",
}


def _mode(value: str) -> HostMode:
    try:
        return _MODES[value]
    except KeyError:
        raise HostdAuthError(f"unknown host mode {value!r}") from None


class HostGateway:
    """Owns the CA-backed listeners and the host registry for the control plane."""

    def __init__(
        self,
        ca: CertificateAuthority,
        registry: HostRegistry,
        tokens: RegistrationTokens,
        *,
        clock: Clock | None = None,
        server_names: Sequence[str] = ("localhost", "127.0.0.1"),
    ) -> None:
        self.ca = ca
        self.registry = registry
        self.tokens = tokens
        self._clock = clock or SystemClock()
        self._server_names = tuple(server_names)
        self._servers: list[grpc.aio.Server] = []
        self.registration_port = 0
        self.gateway_port = 0

    def control_credentials(self) -> ClientCredentials:
        """Credentials the control plane presents when it calls daemons (RemoteProvider)."""
        return self.ca.issue_credentials("control-plane")

    async def start(
        self, host: str = "127.0.0.1", *, registration_port: int = 0, gateway_port: int = 0
    ) -> None:
        identity = self.ca.issue_credentials(
            "hostd-gateway", names=self._server_names, client=False, server=True
        )
        registration = grpc.aio.server(options=SERVER_OPTIONS)
        add_registration_service(_RegistrationService(self), registration)
        self.registration_port = registration.add_secure_port(
            f"{host}:{registration_port}",
            grpc.ssl_server_credentials([(identity.key_pem, identity.cert_pem)]),
        )
        mtls = grpc.aio.server(options=SERVER_OPTIONS)
        add_gateway_service(_GatewayService(self), mtls)
        self.gateway_port = mtls.add_secure_port(
            f"{host}:{gateway_port}", identity.server_credentials()
        )
        self._servers = [registration, mtls]
        await asyncio.gather(*(server.start() for server in self._servers))

    async def stop(self, grace: float | None = None) -> None:
        await asyncio.gather(*(server.stop(grace) for server in self._servers))
        self._servers = []

    async def monitor(self, interval_s: float = 5.0) -> None:
        """Run the liveness sweep until cancelled."""
        while True:
            self.registry.sweep()
            await self._clock.sleep(interval_s)
