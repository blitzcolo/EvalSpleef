from __future__ import annotations

import time
from typing import TYPE_CHECKING

import grpc
import pytest

from evalspleef.hostd._pb import hostd_pb2 as pb
from evalspleef.hostd.rpcerrors import ERROR_METADATA_KEY, HostdAuthError
from evalspleef.hostd.security import (
    CERT_LIFETIME_S,
    CertificateAuthority,
    RegistrationTokens,
    certificate_not_after,
    generate_key_and_csr,
    needs_rotation,
)
from evalspleef.hostd.stubs import hostd_stub
from evalspleef.testing.clock import FakeClock

if TYPE_CHECKING:
    from conftest import SpecMaker, Stacks

CALL_TIMEOUT_S = 10


async def _raw_capabilities(
    address: str, credentials: grpc.ChannelCredentials
) -> grpc.aio.AioRpcError:
    async with grpc.aio.secure_channel(address, credentials) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await hostd_stub(channel).Capabilities(
                pb.ProviderRef(provider="mock"), timeout=CALL_TIMEOUT_S
            )
    return caught.value


async def test_client_without_a_certificate_is_rejected(stacks: Stacks) -> None:
    stack, _ = await stacks.start()
    anonymous = grpc.ssl_channel_credentials(root_certificates=stack.ca.cert_pem)
    error = await _raw_capabilities(f"127.0.0.1:{stack.service_port}", anonymous)
    assert error.code() == grpc.StatusCode.UNAVAILABLE


async def test_client_certificate_from_another_ca_is_rejected(stacks: Stacks) -> None:
    stack, _ = await stacks.start()
    foreign = CertificateAuthority.create("foreign").issue_credentials("control-plane")
    credentials = grpc.ssl_channel_credentials(
        root_certificates=stack.ca.cert_pem,
        private_key=foreign.key_pem,
        certificate_chain=foreign.cert_pem,
    )
    error = await _raw_capabilities(f"127.0.0.1:{stack.service_port}", credentials)
    assert error.code() == grpc.StatusCode.UNAVAILABLE


async def test_expired_client_certificate_is_rejected(stacks: Stacks) -> None:
    stack, _ = await stacks.start()
    expired = stack.ca.issue_credentials(
        "control-plane", issued_at=time.time() - 40 * 86400, lifetime_s=CERT_LIFETIME_S
    )
    assert certificate_not_after(expired.cert_pem) < time.time()
    error = await _raw_capabilities(
        f"127.0.0.1:{stack.service_port}", expired.channel_credentials()
    )
    assert error.code() == grpc.StatusCode.UNAVAILABLE


async def test_valid_certificate_of_another_identity_is_refused(
    stacks: Stacks, spec: SpecMaker
) -> None:
    stack, mock = await stacks.start()
    host_certificate = stack.ca.issue_credentials("some-other-host")
    error = await _raw_capabilities(
        f"127.0.0.1:{stack.service_port}", host_certificate.channel_credentials()
    )
    assert error.code() == grpc.StatusCode.PERMISSION_DENIED
    assert dict(error.trailing_metadata())[ERROR_METADATA_KEY] == "HostdAuthError"
    impostor = stack.remote("mock", credentials=host_certificate)
    with pytest.raises(HostdAuthError):
        await impostor.preflight()
    with pytest.raises(HostdAuthError):
        await impostor.create(spec())
    assert mock.created_sandboxes() == []


async def test_gateway_requires_a_certificate_for_heartbeats(stacks: Stacks) -> None:
    stack, _ = await stacks.start()
    from evalspleef.hostd.stubs import gateway_stub

    anonymous = grpc.ssl_channel_credentials(root_certificates=stack.ca.cert_pem)
    async with grpc.aio.secure_channel(f"127.0.0.1:{stack.gateway.gateway_port}", anonymous) as ch:
        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await gateway_stub(ch).Heartbeat(
                pb.HeartbeatRequest(host_id="host-1", inventory=pb.HostInventory(mode="active")),
                timeout=CALL_TIMEOUT_S,
            )
    assert caught.value.code() == grpc.StatusCode.UNAVAILABLE


async def test_host_cannot_heartbeat_for_another_host(stacks: Stacks) -> None:
    stack, _ = await stacks.start()
    other = stack.ca.issue_credentials("host-2")
    async with grpc.aio.secure_channel(
        f"127.0.0.1:{stack.gateway.gateway_port}", other.channel_credentials()
    ) as channel:
        from evalspleef.hostd.stubs import gateway_stub

        with pytest.raises(grpc.aio.AioRpcError) as caught:
            await gateway_stub(channel).Heartbeat(
                pb.HeartbeatRequest(host_id="host-1", inventory=pb.HostInventory(mode="active")),
                timeout=CALL_TIMEOUT_S,
            )
    assert caught.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_registration_token_is_one_time_and_bound_to_the_host(stacks: Stacks) -> None:
    from evalspleef.hostd.daemon import ControlLink, HostDaemon
    from evalspleef.hostd.loopback import free_port

    stack, _ = await stacks.start()
    link = ControlLink(
        registration_address=f"127.0.0.1:{stack.gateway.registration_port}",
        gateway_address=f"127.0.0.1:{stack.gateway.gateway_port}",
        ca_pem=stack.ca.cert_pem,
    )
    address = f"127.0.0.1:{free_port()}"
    token = stack.tokens.issue("host-7")
    wrong_host = HostDaemon("host-8")
    with pytest.raises(HostdAuthError):
        await wrong_host.register(link, token, listen_address=address)
    # The mismatching attempt spent the token.
    right_host = HostDaemon("host-7")
    with pytest.raises(HostdAuthError):
        await right_host.register(link, token, listen_address=address)
    fresh = stack.tokens.issue("host-7")
    credentials = await right_host.register(link, fresh, listen_address=address)
    assert certificate_not_after(credentials.cert_pem) - time.time() == pytest.approx(
        CERT_LIFETIME_S, abs=600
    )
    with pytest.raises(HostdAuthError):
        await HostDaemon("host-7").register(link, fresh, listen_address=address)
    assert stack.registry.get("host-7").service_address == address


async def test_registration_without_trusting_the_gateway_ca_fails(stacks: Stacks) -> None:
    from evalspleef.hostd.daemon import ControlLink, HostDaemon
    from evalspleef.spi.errors import SandboxCreateError

    stack, _ = await stacks.start()
    link = ControlLink(
        registration_address=f"127.0.0.1:{stack.gateway.registration_port}",
        gateway_address=f"127.0.0.1:{stack.gateway.gateway_port}",
        ca_pem=CertificateAuthority.create("someone-else").cert_pem,
    )
    with pytest.raises(SandboxCreateError):
        await HostDaemon("host-9").register(
            link, stack.tokens.issue("host-9"), listen_address="127.0.0.1:1"
        )


def test_token_expires_and_unknown_tokens_fail() -> None:
    clock = FakeClock()
    tokens = RegistrationTokens(clock)
    token = tokens.issue("h", ttl_s=60)
    clock.advance(61)
    with pytest.raises(HostdAuthError, match="expired"):
        tokens.redeem(token, "h")
    with pytest.raises(HostdAuthError, match="unknown"):
        tokens.redeem("nope", "h")


def test_signed_certificate_is_thirty_days_and_carries_the_host_identity() -> None:
    ca = CertificateAuthority.create()
    _, csr = generate_key_and_csr("ignored-by-the-ca")
    now = time.time()
    cert_pem = ca.sign_csr(
        csr, common_name="host-1", names=["127.0.0.1", "mini.lan"], issued_at=now
    )
    assert certificate_not_after(cert_pem) - now == pytest.approx(CERT_LIFETIME_S, abs=1)
    from cryptography import x509

    cert = x509.load_pem_x509_certificate(cert_pem)
    assert cert.subject.rfc4514_string() == "CN=host-1"
    names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(n.value) for n in names] == ["127.0.0.1", "mini.lan"]


def test_rotation_margin() -> None:
    assert needs_rotation(not_after=1000, now=1000 - 7 * 86400)
    assert not needs_rotation(not_after=1000 + 7 * 86400 + 1, now=1000)
    assert needs_rotation(not_after=1000, now=2000)
