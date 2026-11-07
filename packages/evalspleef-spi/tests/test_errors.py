from __future__ import annotations

import pytest

from evalspleef.spi import (
    CapabilityNotSupported,
    EndpointMismatch,
    EndpointUnavailable,
    PolicyNotEnforceable,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
    SpleefError,
    TransferError,
)

TAXONOMY = [
    (CapabilityNotSupported, None, "manual", False),
    (PolicyNotEnforceable, "environment", "manual", False),
    (QuotaExceeded, "environment", "retry", True),
    (SandboxCreateError, "environment", "retry", True),
    (SandboxLost, "environment", "discard", False),
    (TransferError, "environment", "retry", True),
    (EndpointUnavailable, "provider", "retry", True),
    (EndpointMismatch, "provider", "discard", False),
]


@pytest.mark.parametrize(("exc", "error_class", "disposition", "retryable"), TAXONOMY)
def test_error_maps_to_platform_taxonomy(
    exc: type[SpleefError], error_class: str | None, disposition: str, retryable: bool
) -> None:
    error = exc("boom", detail={"k": "v"})
    assert (exc.error_class, exc.disposition, exc.retryable) == (
        error_class,
        disposition,
        retryable,
    )
    assert error.message == "boom"
    assert error.detail == {"k": "v"}


def test_optional_sandbox_operations_raise_capability_not_supported() -> None:
    import asyncio

    from evalspleef.spi import NetMode, NetworkPolicy, Sandbox

    class Bare(Sandbox):
        async def exec(self, cmd, **kwargs):  # type: ignore[no-untyped-def,override]
            raise NotImplementedError

        async def upload(self, local, remote):  # type: ignore[no-untyped-def,override]
            raise NotImplementedError

        async def download(self, remote, local, *, max_bytes=None):  # type: ignore[no-untyped-def,override]
            raise NotImplementedError

        async def destroy(self) -> None:
            return None

    sandbox = Bare()
    for call in (
        lambda: sandbox.snapshot("x"),
        lambda: sandbox.set_network(NetworkPolicy(mode=NetMode.OFF)),
        lambda: sandbox.gui_endpoint(),
        lambda: sandbox.expose_port(8080),
    ):
        with pytest.raises(CapabilityNotSupported):
            asyncio.run(call())
