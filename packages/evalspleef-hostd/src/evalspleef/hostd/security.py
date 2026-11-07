"""mTLS for hostd (TECH-DESIGN section 7.5).

The control plane keeps a certificate authority. A host exchanges a one-time registration token
for a client certificate (30 days) signed from a CSR, so its private key never leaves the host.
Everything after registration runs over mutually authenticated TLS in both directions.
"""

# mypy: disable-error-code="import-untyped"
from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import grpc
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .clock import Clock, SystemClock
from .rpcerrors import HostdAuthError

CERT_LIFETIME_S = 30 * 24 * 3600
ROTATION_MARGIN_S = 7 * 24 * 3600
CONTROL_PLANE_IDENTITY = "control-plane"
"""Common name of the certificate the control plane presents to hostd services."""

_CA_LIFETIME_S = 10 * 365 * 24 * 3600
_BACKDATE_S = 300


@dataclass(frozen=True)
class ClientCredentials:
    """Everything one side needs to open mTLS channels: its key, its certificate, the CA."""

    ca_pem: bytes
    cert_pem: bytes
    key_pem: bytes

    def channel_credentials(self) -> Any:
        return grpc.ssl_channel_credentials(
            root_certificates=self.ca_pem,
            private_key=self.key_pem,
            certificate_chain=self.cert_pem,
        )

    def server_credentials(self) -> Any:
        return grpc.ssl_server_credentials(
            [(self.key_pem, self.cert_pem)], root_certificates=self.ca_pem, require_client_auth=True
        )

    def server_configuration(self) -> Any:
        return grpc.ssl_server_certificate_configuration(
            [(self.key_pem, self.cert_pem)], root_certificates=self.ca_pem
        )

    @property
    def not_after(self) -> float:
        return certificate_not_after(self.cert_pem)


def _utc(timestamp: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(timestamp, tz=dt.UTC)


def _new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _key_pem(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _alt_names(names: Sequence[str]) -> list[x509.GeneralName]:
    result: list[x509.GeneralName] = []
    for name in names:
        try:
            result.append(x509.IPAddress(ipaddress.ip_address(name)))
        except ValueError:
            result.append(x509.DNSName(name))
    return result


def generate_key_and_csr(common_name: str) -> tuple[bytes, bytes]:
    """A fresh private key (PEM) and a PKCS#10 request for it (PEM)."""
    key = _new_key()
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .sign(key, hashes.SHA256())
    )
    return _key_pem(key), csr.public_bytes(serialization.Encoding.PEM)


def certificate_not_after(cert_pem: bytes) -> float:
    return x509.load_pem_x509_certificate(cert_pem).not_valid_after_utc.timestamp()


def needs_rotation(not_after: float, now: float, *, margin_s: float = ROTATION_MARGIN_S) -> bool:
    """True once the certificate is within ``margin_s`` of expiry (or already expired)."""
    return not_after - now <= margin_s


def peer_common_name(context: Any) -> str | None:
    """Common name of the verified client certificate, ``None`` for an unauthenticated peer."""
    values = context.auth_context().get("x509_common_name")
    if not values:
        return None
    return bytes(values[0]).decode("utf-8")


class CertificateAuthority:
    """The control plane's CA: signs host, server and control-plane certificates."""

    def __init__(
        self, key: ec.EllipticCurvePrivateKey, certificate: x509.Certificate, clock: Clock
    ) -> None:
        self._key = key
        self._certificate = certificate
        self._clock = clock

    @classmethod
    def create(
        cls, common_name: str = "evalspleef-hostd-ca", *, clock: Clock | None = None
    ) -> CertificateAuthority:
        clock = clock or SystemClock()
        key = _new_key()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = clock.now()
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_utc(now - _BACKDATE_S))
            .not_valid_after(_utc(now + _CA_LIFETIME_S))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        return cls(key, certificate, clock)

    @classmethod
    def load(
        cls, cert_pem: bytes, key_pem: bytes, *, clock: Clock | None = None
    ) -> CertificateAuthority:
        key = serialization.load_pem_private_key(key_pem, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise HostdAuthError("the CA key must be an EC key")
        return cls(key, x509.load_pem_x509_certificate(cert_pem), clock or SystemClock())

    @property
    def cert_pem(self) -> bytes:
        return self._certificate.public_bytes(serialization.Encoding.PEM)

    @property
    def key_pem(self) -> bytes:
        return _key_pem(self._key)

    def _sign(
        self,
        public_key: Any,
        *,
        common_name: str,
        names: Sequence[str],
        client: bool,
        server: bool,
        issued_at: float | None,
        lifetime_s: float,
    ) -> x509.Certificate:
        now = self._clock.now() if issued_at is None else issued_at
        usages = []
        if client:
            usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
        if server:
            usages.append(ExtendedKeyUsageOID.SERVER_AUTH)
        builder = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(self._certificate.subject)
            .public_key(public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(_utc(now - _BACKDATE_S))
            .not_valid_after(_utc(now + lifetime_s))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage(usages), critical=False)
        )
        if names:
            builder = builder.add_extension(
                x509.SubjectAlternativeName(_alt_names(names)), critical=False
            )
        return builder.sign(self._key, hashes.SHA256())

    def sign_csr(
        self,
        csr_pem: bytes,
        *,
        common_name: str,
        names: Sequence[str] = (),
        issued_at: float | None = None,
        lifetime_s: float = CERT_LIFETIME_S,
    ) -> bytes:
        """Issue a client+server certificate for the CSR's key under the identity we choose."""
        try:
            csr = x509.load_pem_x509_csr(csr_pem)
        except ValueError as exc:
            raise HostdAuthError(f"malformed certificate request: {exc}") from exc
        if not csr.is_signature_valid:
            raise HostdAuthError("certificate request signature is invalid")
        certificate = self._sign(
            csr.public_key(),
            common_name=common_name,
            names=names,
            client=True,
            server=True,
            issued_at=issued_at,
            lifetime_s=lifetime_s,
        )
        return certificate.public_bytes(serialization.Encoding.PEM)

    def issue_credentials(
        self,
        common_name: str,
        *,
        names: Sequence[str] = (),
        client: bool = True,
        server: bool = False,
        issued_at: float | None = None,
        lifetime_s: float = CERT_LIFETIME_S,
    ) -> ClientCredentials:
        """Key and certificate for a party that holds its key on the control-plane side."""
        key = _new_key()
        certificate = self._sign(
            key.public_key(),
            common_name=common_name,
            names=names,
            client=client,
            server=server,
            issued_at=issued_at,
            lifetime_s=lifetime_s,
        )
        return ClientCredentials(
            ca_pem=self.cert_pem,
            cert_pem=certificate.public_bytes(serialization.Encoding.PEM),
            key_pem=_key_pem(key),
        )


class RegistrationTokens:
    """One-time registration tokens bound to a host id; only their hashes are kept."""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self._pending: dict[str, tuple[str, float]] = {}

    @staticmethod
    def _digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def issue(self, host_id: str, *, ttl_s: float = 3600) -> str:
        token = secrets.token_urlsafe(32)
        self._pending[self._digest(token)] = (host_id, self._clock.now() + ttl_s)
        return token

    def redeem(self, token: str, host_id: str) -> None:
        """Consume the token; it is spent even when the presented host id does not match."""
        entry = self._pending.pop(self._digest(token), None)
        if entry is None:
            raise HostdAuthError("unknown or already used registration token")
        bound_host, expires_at = entry
        if self._clock.now() > expires_at:
            raise HostdAuthError("registration token expired")
        if bound_host != host_id:
            raise HostdAuthError("registration token was issued for another host")
