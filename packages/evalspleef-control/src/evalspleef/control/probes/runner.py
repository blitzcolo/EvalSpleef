"""ProbeRunner: executes the probe suite through the gateway with a dedicated probe key."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace

import httpx

from evalspleef.spi.gateway import GatewayAdmin
from evalspleef.spi.probe import ProbeReport

from .client import CHAT_PATH, GatewayClient
from .clock import Clock, SystemClock, iso_utc
from .config import ProbeConfig, ProbeTarget
from .report import build_report
from .suite import ProbeSuite

__all__ = ["PingResult", "ProbeRunner"]

_PING_BUDGET_USD = 1.0
_PING_TOKEN_CAP = 1_000


@dataclass(frozen=True)
class PingResult:
    """Outcome of one ``max_tokens=1`` health request through the gateway."""

    ok: bool
    status: int | None
    latency_s: float
    cold_start_ms: int = 0
    error: str = ""


class ProbeRunner:
    """Runs P-01..P-15 against a route that is already registered on the gateway.

    ``gateway_url`` is the gateway data-plane root (for example ``http://gateway:8080``).
    Probe keys are short-lived and revoked when the run ends.
    """

    def __init__(
        self,
        admin: GatewayAdmin,
        *,
        gateway_url: str,
        clock: Clock | None = None,
        config: ProbeConfig | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._admin = admin
        self._gateway_url = gateway_url
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._config = config if config is not None else ProbeConfig()
        self._transport = transport

    @asynccontextmanager
    async def _session(
        self,
        endpoint_id: str,
        purpose: str,
        *,
        budget_usd: float,
        token_cap: int,
        request_timeout_s: float,
    ) -> AsyncIterator[GatewayClient]:
        now = self._clock.now()
        key = await self._admin.issue_key(
            trial_id=f"{purpose}-{endpoint_id}-{int(now)}",
            endpoint_id=endpoint_id,
            max_budget_usd=budget_usd,
            max_tokens=token_cap,
            expires_at=iso_utc(now + self._config.key_ttl_s),
        )
        try:
            async with GatewayClient(
                self._gateway_url,
                key.secret,
                clock=self._clock,
                request_timeout_s=request_timeout_s,
                connect_timeout_s=self._config.connect_timeout_s,
                transport=self._transport,
            ) as client:
                yield client
        finally:
            await self._admin.revoke_key(key.key_id)

    async def run(
        self, target: ProbeTarget, *, request_timeout_s: float | None = None
    ) -> ProbeReport:
        """Run the suite and return the immutable report (not persisted here).

        request_timeout_s replaces the ordinary read timeout for this run only, so a
        deployment can raise it without changing the long-request timeout of P-09/P-10.
        """
        config = (
            self._config
            if request_timeout_s is None
            else replace(self._config, request_timeout_s=request_timeout_s)
        )
        async with self._session(
            target.endpoint_id,
            "probe",
            budget_usd=config.key_budget_usd,
            token_cap=config.key_token_cap,
            request_timeout_s=config.request_timeout_s,
        ) as client:
            suite = ProbeSuite(target, client, config, now=self._clock.now)
            checks = await suite.run()
        return build_report(
            target.endpoint_id,
            await self._admin.version(),
            iso_utc(self._clock.now()),
            checks,
        )

    async def ping(
        self, endpoint_id: str, model_name: str, *, timeout_s: float | None = None
    ) -> PingResult:
        """One minimal request (``max_tokens=1``) used for health checks and warm-up."""
        timeout = timeout_s if timeout_s is not None else self._config.request_timeout_s
        async with self._session(
            endpoint_id,
            "ping",
            budget_usd=_PING_BUDGET_USD,
            token_cap=_PING_TOKEN_CAP,
            request_timeout_s=timeout,
        ) as client:
            started = self._clock.now()
            try:
                reply = await client.post(
                    CHAT_PATH,
                    {
                        "model": model_name,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        "temperature": 0,
                    },
                )
            except httpx.HTTPError as exc:
                return PingResult(
                    ok=False,
                    status=None,
                    latency_s=self._clock.now() - started,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return PingResult(
            ok=reply.ok,
            status=reply.status,
            latency_s=reply.latency_s,
            cold_start_ms=reply.cold_start_ms,
            error="" if reply.ok else reply.error,
        )
