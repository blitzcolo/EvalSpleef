"""Conformance target for the OpenAI-compatible adapter.

The target talks to the real HTTP ``FakeOpenAIServer`` from ``evalspleef-testing`` on
127.0.0.1: a genuine OpenAI-compatible wire surface, scriptable for faults, but a double
rather than a vendor host, so the target is marked ``fake=True``.

This module is the one place the adapter may import the conformance kit and the test doubles
(the "adapters depend on spi only" import contract makes exactly this exception).

Fault injectors:

- ``upstream_status`` (``status``, ``count`` or ``seconds``): make the upstream answer with
  an injected HTTP status (429/500/503 ship an error body; other statuses are registered
  with the double at injection time) for the next calls or for a clock window;
- ``kill_host``: stop the upstream server, as if the endpoint host vanished.
"""

from __future__ import annotations

from collections.abc import Callable

from evalspleef.conformance.target import ConformanceTarget, ModelFixture
from evalspleef.spi.model import DeploymentSpec, ModelSpec, VendorSource
from evalspleef.testing import fake_openai
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.secrets import DictSecretResolver

from .provider import OpenAICompatibleProvider

_MODEL = "fake-model"
_API_KEY = "sk-openai-compat-conformance"
_SECRET_REF = "vault://models/fake-model"


def _spec_factory(server: FakeOpenAIServer) -> Callable[[], ModelSpec]:
    def factory() -> ModelSpec:
        # The server is started lazily and idempotently so a case may build its spec
        # before opening the upstream itself.
        server.start()
        return ModelSpec(
            slug=_MODEL,
            spec_version=1,
            family="fake",
            roles=frozenset({"target"}),
            source=VendorSource(vendor="openai-compat", model_id=_MODEL),
            deployment=DeploymentSpec(
                adapter="openai-compatible",
                account="openai-compat-fake",
                params={
                    "base_url": server.base_url,
                    "model": _MODEL,
                    "api_key_ref": _SECRET_REF,
                    "protocol": "chat_completions",
                },
            ),
            declared_context_tokens=8192,
            declared_max_output_tokens=1024,
            pricing_ref="fake",
            license="proprietary",
            owner="evalspleef",
        )

    return factory


def build_targets() -> list[ConformanceTarget]:
    """The ``openai-compat-fake`` model target, backed by the fake OpenAI server."""
    server = FakeOpenAIServer(model=_MODEL, api_key=_API_KEY)
    secrets = DictSecretResolver({_SECRET_REF: _API_KEY})
    provider = OpenAICompatibleProvider(
        "openai-compat-fake",
        {
            "api_key_ref": _SECRET_REF,
            "protocol": "chat_completions",
            "wake_retry_interval_s": 0.02,
        },
        secrets,
    )

    async def upstream_status(
        status: int = 503, *, count: int = 1, seconds: float | None = None
    ) -> None:
        # The double ships bodies only for 429/500/503; the kit also injects 401/403/404/422
        # to check error mapping, so register a body for any status before arming it.
        fake_openai._ERRORS.setdefault(status, ("invalid_request_error", f"injected_{status}"))
        if seconds is not None:
            server.inject_error_for(status, seconds=seconds)
        else:
            server.inject_error(status, count=count)

    async def kill_host() -> None:
        server.stop()

    async def cleanup() -> None:
        server.stop()

    fixture = ModelFixture(
        spec_factory=_spec_factory(server),
        operator_input_factory=None,
        upstream=server,
    )
    target = ConformanceTarget(
        name="openai-compat-fake",
        kind="model",
        provider=provider,
        fake=True,
        injectors={"upstream_status": upstream_status, "kill_host": kill_host},
        model_fixture=fixture,
        notes=(
            "real HTTP against the evalspleef-testing FakeOpenAIServer; the upstream is a "
            "test double, not a vendor host"
        ),
        cleanup=cleanup,
    )
    return [target]
