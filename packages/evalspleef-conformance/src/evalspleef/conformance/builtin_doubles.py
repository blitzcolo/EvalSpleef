"""Production doubles shared by the builtin model conformance targets.

The doubles here fake the *vendor side* of a model provider so the builtin
``mock-model-*`` targets can exercise the fault paths (status mapping, assisted
403, HF preflight) without a real host. ``builtin.build_targets`` wires them up;
the model case tests import them too so there is exactly one copy.

``FakeOpenAIServer`` already binds its port in ``start``; ``LazyUpstreamServer``
adds the one thing the builtin targets need: the port is only bound when a case
first asks for ``base_url``, so discovering the targets starts nothing.
"""

from __future__ import annotations

from collections.abc import Mapping

from evalspleef.conformance.cases_model import (
    HF_BIN_ONLY_REPO,
    HF_GATED_REPO,
    HF_PRIVATE_REPO,
    HF_PUBLIC_REPO,
)
from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    EndpointHandle,
    EndpointState,
    HFSource,
    ValidationReport,
)
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.mock_endpoint import MockApiProvider, MockAssistedProvider


class LazyUpstreamServer(FakeOpenAIServer):
    """OpenAI-compatible upstream that starts on first use instead of at construction."""

    @property
    def base_url(self) -> str:
        """Start the server if needed, then return its base URL."""
        self.start()
        return super().base_url


class StatusMappingProvider(MockApiProvider):
    """Api-mode double that maps an injected upstream status onto the SPI error taxonomy."""

    injected_status: int | None = None
    swallow: bool = False

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        del timeout_s
        status = self.injected_status
        self.injected_status = None
        if self.swallow or status is None:
            return handle.state
        if status in (429, 500, 503):
            raise EndpointUnavailable(f"upstream {status}", detail={"status": status})
        raise EndpointMismatch(f"upstream {status}", detail={"status": status})


class FaultAssistedProvider(MockAssistedProvider):
    """Assisted double that surfaces a scripted vendor 401/403 from ``attach``."""

    injected_status: int | None = None

    async def attach(
        self, handle: EndpointHandle, operator_input: Mapping[str, str]
    ) -> EndpointHandle:
        status = self.injected_status
        self.injected_status = None
        if status in (401, 403):
            raise EndpointMismatch(
                "the vendor rejected the attached deployment", detail={"status": status}
            )
        return await super().attach(handle, operator_input)


class HFDouble(HFAccessChecker):
    """HF access double for the four fixture repos M-HF-01 probes."""

    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        del repo, token_ref
        return revision

    async def preflight(self, source: HFSource) -> ValidationReport:
        if source.repo == HF_PUBLIC_REPO:
            return ValidationReport(ok=True, checks=(("readable", True, "public repo readable"),))
        if source.repo == HF_GATED_REPO:
            return ValidationReport(
                ok=False,
                checks=(("licence", False, "gated repository licence has not been accepted"),),
            )
        if source.repo == HF_PRIVATE_REPO:
            return ValidationReport(
                ok=False,
                checks=(
                    (
                        "access",
                        False,
                        "private repository is not readable by the deployment identity",
                    ),
                ),
            )
        if source.repo == HF_BIN_ONLY_REPO:
            return ValidationReport(
                ok=False,
                checks=(
                    (
                        "weights_format",
                        False,
                        "repository ships .bin weights only; safetensors are required",
                    ),
                ),
            )
        raise AssertionError(f"unexpected HF repo {source.repo!r}")

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        del source
        return {}

    async def ensure_mirror(self, source: HFSource) -> str:
        return source.mirror_repo or source.repo

    async def verify_mirror(self, source: HFSource) -> bool:
        del source
        return True
