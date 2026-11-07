"""Typed entry points into the untyped generated gRPC module."""

# mypy: disable-error-code="import-untyped,no-untyped-call"
from __future__ import annotations

from typing import Any

from ._pb import hostd_pb2_grpc as pb_grpc


def hostd_stub(channel: Any) -> Any:
    return pb_grpc.HostdStub(channel)


def registration_stub(channel: Any) -> Any:
    return pb_grpc.HostRegistrationStub(channel)


def gateway_stub(channel: Any) -> Any:
    return pb_grpc.HostGatewayStub(channel)


def add_hostd_service(service: Any, server: Any) -> None:
    pb_grpc.add_HostdServicer_to_server(service, server)


def add_registration_service(service: Any, server: Any) -> None:
    pb_grpc.add_HostRegistrationServicer_to_server(service, server)


def add_gateway_service(service: Any, server: Any) -> None:
    pb_grpc.add_HostGatewayServicer_to_server(service, server)
