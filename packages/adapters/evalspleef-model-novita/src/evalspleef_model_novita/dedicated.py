"""``novita-dedicated``: Novita Dedicated Endpoint in assisted mode (TECH-DESIGN section 6.6.2).

Novita offers no management API for dedicated endpoints (PRD section 8.1), so an operator
creates the deployment in the console from the instructions this provider renders, then pastes
``base_url``, ``model`` and the deployment id back through ``attach``. From then on the state
is inferred from the inference plane only.

Everything here is *not verified against a real Novita account*.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from evalspleef.spi.errors import EndpointMismatch, EndpointUnavailable
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import (
    DeploymentPlan,
    EndpointHandle,
    EndpointState,
    HFSource,
    ManagementMode,
    ModelEndpointProvider,
    ModelSpec,
    TeardownMode,
    UpstreamRoute,
    ValidationReport,
)
from evalspleef.spi.secrets import SecretResolver

from .clock import Clock, SystemClock
from .errors import CredentialRejected, InvalidBaseUrl, ModelNameMismatch, PlanInputMissing
from .inference import (
    DEFAULT_NOT_READY_MARKERS,
    ProbeResult,
    probe,
    raise_for_probe,
    wake_loop,
)
from .sizing import (
    GPU_CATALOG,
    GpuChoice,
    GpuSku,
    ModelShape,
    apply_price_overrides,
    choose_gpu,
    estimate_vram_gb,
    replicas_for,
)

COLD_START_WINDOW_S = 480
"""Novita documents about five minutes for a cold start; the gateway retries for eight."""
SLOW_FIRST_ANSWER_S = 60.0
TICKET_TTL_S = 24 * 3600
SCALE_DOWN_DELAY_S = 300
DEFAULT_MAX_CONCURRENCY = 16
DEFAULT_PLANNED_HOURS = 14.0
DEFAULT_WAKE_POLL_S = 15.0
DEFAULT_PROBE_TIMEOUT_S = 90.0

_SHA = re.compile(r"^[0-9a-f]{40}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REQUIRED_SHAPE_PARAMS = ("num_hidden_layers", "num_key_value_heads", "head_dim")


def _endpoint_id(spec: ModelSpec) -> str:
    digest = hashlib.sha256(f"{spec.slug}:{spec.spec_version}".encode()).hexdigest()[:8]
    return f"ep_{spec.slug}_{digest}"


class NovitaDedicatedProvider(ModelEndpointProvider):
    """Assisted-mode provider for Novita Dedicated Endpoints."""

    name = "novita-dedicated"
    management_mode = ManagementMode.ASSISTED
    hf_checker: HFAccessChecker | None = None
    """Assigned by the control plane after construction; ``validate`` fails without it."""

    def __init__(
        self,
        account: str,
        config: Mapping[str, Any],
        secrets: SecretResolver,
        *,
        clock: Clock | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        super().__init__(account, config, secrets)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._transport = transport

    # -- configuration -------------------------------------------------------------------

    def _credential_ref(self, spec: ModelSpec) -> str:
        for value in (spec.deployment.params.get("api_key_ref"), self.config.get("api_key_ref")):
            if isinstance(value, str) and value:
                return value
        suffix = re.sub(r"[^A-Z0-9]", "_", self.account.upper())
        return f"env://NOVITA_API_KEY_{suffix}"

    def _float(self, key: str, default: float) -> float:
        return float(self.config.get(key, default))

    def _markers(self) -> tuple[str, ...]:
        raw = self.config.get("not_ready_markers")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(marker).lower() for marker in raw)
        return DEFAULT_NOT_READY_MARKERS

    def _catalog(self) -> tuple[GpuSku, ...]:
        raw = self.config.get("gpu_prices")
        if isinstance(raw, Mapping):
            return apply_price_overrides(GPU_CATALOG, {str(k): float(v) for k, v in raw.items()})
        return GPU_CATALOG

    # -- validate ------------------------------------------------------------------------

    async def validate(self, spec: ModelSpec) -> ValidationReport:
        checks: list[tuple[str, bool, str]] = []
        credential_ref = self._credential_ref(spec)
        try:
            resolved = bool(self.secrets.resolve(credential_ref))
            detail = credential_ref if resolved else f"{credential_ref} resolves to an empty value"
        except (KeyError, ValueError) as exc:
            resolved, detail = False, f"{credential_ref} does not resolve ({type(exc).__name__})"
        checks.append(("novita_key", resolved, detail))

        source = spec.source
        if not isinstance(source, HFSource):
            checks.append(("model_source", True, f"vendor catalog model {source.model_id}"))
            return self._report(checks)

        checks.append(
            (
                "revision_pinned",
                bool(_SHA.match(source.revision)),
                f"revision {source.revision!r} must be a 40-hex commit sha",
            )
        )
        checker = self.hf_checker
        if checker is None:
            checks.append(
                (
                    "hf_checker_missing",
                    False,
                    "no HFAccessChecker injected; the control plane must assign hf_checker",
                )
            )
        else:
            hf_report = await checker.preflight(source)
            checks.extend((f"hf_{check_id}", ok, text) for check_id, ok, text in hf_report.checks)
            if not hf_report.checks:
                checks.append(("hf_preflight", hf_report.ok, "preflight returned no checks"))
        checks.append(self._safetensors_check(spec, checker is not None and self._hf_ok(checks)))
        return self._report(checks)

    @staticmethod
    def _hf_ok(checks: list[tuple[str, bool, str]]) -> bool:
        return all(ok for check_id, ok, _ in checks if check_id.startswith("hf_"))

    @staticmethod
    def _report(checks: list[tuple[str, bool, str]]) -> ValidationReport:
        return ValidationReport(ok=all(ok for _, ok, _ in checks), checks=tuple(checks))

    @staticmethod
    def _safetensors_check(spec: ModelSpec, preflight_ok: bool) -> tuple[str, bool, str]:
        files = spec.deployment.params.get("weight_files")
        if not isinstance(files, (list, tuple)) or not files:
            return (
                "safetensors_weights",
                preflight_ok,
                "no weight file list supplied; relying on the HF preflight",
            )
        names = [str(name) for name in files]
        bad = [name for name in names if not name.endswith(".safetensors")]
        if bad:
            return (
                "safetensors_weights",
                False,
                f"Novita dedicated endpoints load .safetensors only; found {bad}",
            )
        return "safetensors_weights", True, f"{len(names)} .safetensors files"

    # -- plan ------------------------------------------------------------------------------

    async def _weight_bytes(self, spec: ModelSpec) -> int:
        params = spec.deployment.params
        if params.get("weight_bytes") is not None:
            return int(params["weight_bytes"])
        sizes = params.get("safetensors_sizes")
        if isinstance(sizes, Mapping) and sizes:
            return sum(int(size) for size in sizes.values())
        if self.hf_checker is not None and isinstance(spec.source, HFSource):
            report = await self.hf_checker.preflight(spec.source)
            for check_id, _ok, text in report.checks:
                if check_id == "weight_bytes" and text.isdigit():
                    return int(text)
        raise PlanInputMissing(
            "weight size unknown: supply params weight_bytes or safetensors_sizes, "
            "or let the HF checker report a 'weight_bytes' check",
            detail={"missing": ["weight_bytes"]},
        )

    async def plan(self, spec: ModelSpec) -> DeploymentPlan:
        params = spec.deployment.params
        missing = [name for name in _REQUIRED_SHAPE_PARAMS if params.get(name) is None]
        if missing:
            raise PlanInputMissing(
                f"KV cache sizing needs the model architecture; missing params {missing}",
                detail={"missing": missing},
            )
        max_conc = int(params.get("max_concurrency_per_replica", DEFAULT_MAX_CONCURRENCY))
        peak = int(params.get("peak_concurrency", max_conc))
        shape = ModelShape(
            weight_bytes=await self._weight_bytes(spec),
            num_hidden_layers=int(params["num_hidden_layers"]),
            num_key_value_heads=int(params["num_key_value_heads"]),
            head_dim=int(params["head_dim"]),
            kv_dtype_bytes=int(params.get("kv_dtype_bytes", 2)),
        )
        vram = estimate_vram_gb(shape, spec.declared_context_tokens, min(peak, max_conc))
        choice = choose_gpu(vram, self._catalog())
        replicas = replicas_for(peak, max_conc)
        unit = choice.sku.price_per_hour_usd
        cost_per_hour = None if unit is None else unit * choice.count * replicas
        hours = float(params.get("planned_hours", DEFAULT_PLANNED_HOURS))
        draft = DeploymentPlan(
            gpu_type=choice.sku.name,
            gpu_count=choice.count,
            replicas_min=replicas,
            replicas_max=replicas,
            max_concurrency=max_conc,
            est_vram_gb=round(vram, 1),
            est_cost_per_hour_usd=cost_per_hour,
        )
        markdown = self._render_instructions(spec, draft, choice, hours)
        return DeploymentPlan(
            gpu_type=draft.gpu_type,
            gpu_count=draft.gpu_count,
            replicas_min=draft.replicas_min,
            replicas_max=draft.replicas_max,
            max_concurrency=draft.max_concurrency,
            est_vram_gb=draft.est_vram_gb,
            est_cost_per_hour_usd=draft.est_cost_per_hour_usd,
            operator_instructions_md=markdown,
        )

    def _render_instructions(
        self, spec: ModelSpec, plan: DeploymentPlan, choice: GpuChoice, hours: float
    ) -> str:
        endpoint_id = _endpoint_id(spec)
        batch = str(spec.deployment.params.get("batch_label", "adhoc"))
        source = spec.source
        if isinstance(source, HFSource):
            repo = source.mirror_repo or source.repo
            model_line = f"Hugging Face -> `{repo}`"
            if source.access == "public":
                token_note = "public repository: no HF token needed"
            else:
                identity = self.config.get("hf_deploy_identity", "the deployment identity")
                token_note = (
                    f"{source.access} repository: confirm that **{identity}** clicked "
                    '"Integrate HF Token" in the console'
                )
            pin = (
                f"   - revision `{source.revision}` is pinned by the mirror repo; the console "
                "has no revision field"
                if source.mirror_repo
                else f"   - WARNING: no frozen mirror; the console cannot pin `{source.revision}`"
            )
            model_block = f"3. Model: {model_line}\n   - {token_note}\n{pin}"
        else:
            model_block = f"3. Model: Novita catalog -> `{source.model_id}`"
        vram = plan.est_vram_gb or 0.0
        headroom = (1 - vram / choice.total_vram_gb) * 100
        replicas = plan.replicas_min
        unit = choice.sku.price_per_hour_usd
        if unit is None:
            cost = "price not in the catalog; read it off the console before approving"
        else:
            hourly = unit * choice.count * replicas
            cost = (
                f"{replicas} x {choice.count} x ${unit:.2f}/h; {hours:g} h = about "
                f"${hourly * hours:.1f} (excluding deploy and warm-up time)"
            )
        return (
            f"## Deployment {endpoint_id} ({spec.slug}, batch {batch})\n"
            "1. Open the Novita console -> Models APIs -> Deployments -> + New Deployment\n"
            f"2. Name: `eval-{batch}-{spec.slug}`\n"
            f"{model_block}\n"
            f"4. GPU: {choice.count} x {choice.sku.name} "
            f"(estimated VRAM {vram:.0f} GB, headroom {headroom:.0f}%)\n"
            f"5. Autoscaling: Min Replicas = {replicas}, Max Replicas = {plan.replicas_max}, "
            f"Scale-down Delay = {SCALE_DOWN_DELAY_S}s\n"
            "   (fixed for the batch run; set Min Replicas to 0 after grading)\n"
            f"6. Max Concurrency per Replica: {plan.max_concurrency}; Suffix Decoding: off\n"
            "7. When the deployment is RUNNING, copy base_url and model from Quick Start and "
            "run:\n"
            f"   `spleef models attach {endpoint_id} --base-url <...> --model <...> "
            "--deployment-id <...>`\n"
            f"Estimated cost: {cost}\n"
        )

    # -- deploy / attach ---------------------------------------------------------------------

    async def deploy(self, spec: ModelSpec, plan: DeploymentPlan) -> EndpointHandle:
        now = self._clock.now()
        instructions = plan.operator_instructions_md or ""
        ticket = {
            "id": "deploy-1",
            "kind": "deploy",
            "text": (
                f"OPERATOR TICKET (deadline {TICKET_TTL_S // 3600} h from opening)\n\n"
                f"{instructions}"
            ),
            "opened_at": now,
            "expires_at": now + TICKET_TTL_S,
        }
        handle = EndpointHandle(
            endpoint_id=_endpoint_id(spec),
            model=spec,
            state=EndpointState.AWAITING_OPERATOR,
            base_url=None,
            upstream_model=None,
            protocol="chat_completions",
            credential_ref=self._credential_ref(spec),
        )
        handle.observed.update(
            {
                "operator_instructions_md": instructions,
                "ticket": ticket,
                "engine": "unknown",
                "plan": {
                    "gpu_type": plan.gpu_type,
                    "gpu_count": plan.gpu_count,
                    "replicas_min": plan.replicas_min,
                    "replicas_max": plan.replicas_max,
                    "max_concurrency": plan.max_concurrency,
                },
            }
        )
        return handle

    def _check_base_url(self, base_url: str) -> None:
        try:
            parsed = urlsplit(base_url)
            host = (parsed.hostname or "").lower()
            userinfo = bool(parsed.username or parsed.password)
        except ValueError as exc:
            raise InvalidBaseUrl(
                f"base_url is not a valid URL: {exc}", detail={"reason": "base_url_invalid"}
            ) from exc
        loopback_ok = bool(self.config.get("allow_loopback_http")) and host in _LOOPBACK_HOSTS
        if parsed.scheme == "http" and loopback_ok:
            return
        if parsed.scheme != "https":
            raise InvalidBaseUrl(
                f"base_url must use https, got scheme {parsed.scheme!r}",
                detail={"reason": "base_url_not_https", "base_url": base_url},
            )
        if userinfo or not host.endswith(".novita.ai"):
            raise InvalidBaseUrl(
                f"base_url host {host!r} is not a *.novita.ai host",
                detail={"reason": "base_url_host", "base_url": base_url},
            )

    def _api_key(self, handle: EndpointHandle) -> str:
        try:
            return self.secrets.resolve(handle.credential_ref)
        except (KeyError, ValueError) as exc:
            raise CredentialRejected(
                f"the secret reference {handle.credential_ref} does not resolve",
                detail={"reason": "secret_unresolved"},
            ) from exc

    async def _probe(self, handle: EndpointHandle, timeout_s: float) -> ProbeResult:
        assert handle.base_url is not None and handle.upstream_model is not None
        return await probe(
            api_base=handle.base_url,
            api_key=self._api_key(handle),
            model=handle.upstream_model,
            clock=self._clock,
            timeout_s=timeout_s,
            not_ready_markers=self._markers(),
            transport=self._transport,
        )

    async def attach(
        self, handle: EndpointHandle, operator_input: Mapping[str, str]
    ) -> EndpointHandle:
        base_url = operator_input.get("base_url", "")
        model = operator_input.get("model", "")
        self._check_base_url(base_url)
        expected = handle.model.deployment.params.get("model")
        if not model or (isinstance(expected, str) and expected and model != expected):
            raise ModelNameMismatch(
                f"attached model {model!r} does not match the registered model {expected!r}",
                detail={"reason": "model_mismatch", "expected": expected, "actual": model},
            )
        api_key = self._api_key(handle)
        result = await probe(
            api_base=base_url,
            api_key=api_key,
            model=model,
            clock=self._clock,
            timeout_s=self._float("attach_timeout_s", 600.0),
            not_ready_markers=self._markers(),
            transport=self._transport,
        )
        raise_for_probe(result, "attach")
        if result.model != model:
            raise ModelNameMismatch(
                f"endpoint answers as model {result.model!r}, not the attached {model!r}",
                detail={"reason": "model_mismatch", "expected": model, "actual": result.model},
            )
        handle.base_url = base_url
        handle.upstream_model = model
        handle.state = EndpointState.VERIFYING
        deployment_id = operator_input.get("deployment_id")
        if deployment_id:
            handle.observed["deployment_id"] = deployment_id
            handle.external_ids["novita_deployment_id"] = deployment_id
        handle.observed["first_response_s"] = result.elapsed_s
        self._note_cold_start(handle, result.elapsed_s)
        return handle

    # -- status / wake -------------------------------------------------------------------------

    @staticmethod
    def _note_cold_start(handle: EndpointHandle, elapsed_s: float) -> None:
        """Record a cold start once the first slow answer is seen (FR-MOD-03 item 3)."""
        if elapsed_s < SLOW_FIRST_ANSWER_S:
            return
        handle.observed.setdefault("first_cold_start_s", elapsed_s)
        handle.observed.setdefault("cold_start_events", []).append({"elapsed_s": elapsed_s})

    async def status(self, handle: EndpointHandle) -> EndpointState:
        if handle.state in (EndpointState.RETIRED, EndpointState.REJECTED):
            return handle.state
        if handle.base_url is None:
            ticket = handle.observed.get("ticket")
            if isinstance(ticket, Mapping) and self._clock.now() > float(ticket["expires_at"]):
                return EndpointState.FAILED
            return EndpointState.AWAITING_OPERATOR
        result = await self._probe(handle, self._float("status_timeout_s", DEFAULT_PROBE_TIMEOUT_S))
        handle.observed["last_status"] = {
            "outcome": result.outcome,
            "http_status": result.http_status,
            "elapsed_s": result.elapsed_s,
            "at": self._clock.now(),
        }
        match result.outcome:
            case "ok":
                if result.model != handle.upstream_model:
                    handle.observed["identity_drift"] = result.model
                    return EndpointState.DEGRADED
                if result.elapsed_s > SLOW_FIRST_ANSWER_S:
                    self._note_cold_start(handle, result.elapsed_s)
                    return EndpointState.SLEEPING
                return EndpointState.WARM
            case "not_ready" | "timeout":
                return EndpointState.SLEEPING
            case "unauthorized" | "forbidden" | "not_found":
                # 401/403: the key is not the deployment owner's; 404: wrong URL or model id.
                return EndpointState.FAILED
            case "rejected" | "unavailable":
                return EndpointState.DEGRADED

    async def wake(self, handle: EndpointHandle, timeout_s: int) -> EndpointState:
        if handle.base_url is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no base_url yet")
        result, elapsed = await wake_loop(
            lambda timeout: self._probe(handle, timeout),
            clock=self._clock,
            timeout_s=timeout_s,
            poll_s=self._float("wake_poll_interval_s", DEFAULT_WAKE_POLL_S),
            probe_cap_s=self._float("status_timeout_s", DEFAULT_PROBE_TIMEOUT_S),
        )
        if result.outcome == "ok":
            handle.observed["last_wake_s"] = elapsed
            self._note_cold_start(handle, elapsed)
            handle.state = EndpointState.WARM
        elif result.outcome in ("not_ready", "timeout"):
            handle.state = EndpointState.SLEEPING
        else:
            if result.outcome in ("unauthorized", "forbidden", "not_found"):
                handle.state = EndpointState.FAILED
            raise_for_probe(result, "wake")
        return handle.state

    # -- scale / teardown tickets --------------------------------------------------------------

    def _open_ticket(self, handle: EndpointHandle, kind: str, text: str, **extra: Any) -> None:
        now = self._clock.now()
        tickets: list[dict[str, Any]] = handle.observed.setdefault("tickets", [])
        ticket = {
            "id": f"{kind}-{len(tickets) + 1}",
            "kind": kind,
            "text": text,
            "opened_at": now,
            "expires_at": now + TICKET_TTL_S,
            **extra,
        }
        tickets.append(ticket)
        handle.observed["pending_ticket"] = ticket

    async def scale(self, handle: EndpointHandle, min_replicas: int, max_replicas: int) -> None:
        deployment = handle.observed.get("deployment_id", handle.endpoint_id)
        self._open_ticket(
            handle,
            "scale",
            f"In the Novita console open deployment `{deployment}` and set "
            f"Min Replicas = {min_replicas}, Max Replicas = {max_replicas}. "
            "Then confirm the ticket so the system can run its health check.",
            min_replicas=min_replicas,
            max_replicas=max_replicas,
        )

    async def teardown(self, handle: EndpointHandle, mode: TeardownMode) -> None:
        if handle.base_url is None:
            handle.state = EndpointState.RETIRED  # nothing was ever deployed
            return
        deployment = handle.observed.get("deployment_id", handle.endpoint_id)
        action = (
            "set Min Replicas = 0 (scale to zero)"
            if mode == "sleep"
            else "click Terminate (GPU type cannot be changed later; do this only after grading)"
        )
        self._open_ticket(
            handle,
            "teardown",
            f"In the Novita console open deployment `{deployment}` and {action}. "
            "Then confirm the ticket so the system can run its health check.",
            mode=mode,
        )

    async def confirm_ticket(self, handle: EndpointHandle) -> EndpointState:
        """Close the pending scale or teardown ticket after the operator confirmed it.

        The control plane calls this once the operator reports the console change. A terminate
        is accepted only when the endpoint answers 404 or 401 afterwards; scale-to-zero is
        accepted on the operator's word, because a health request would wake the endpoint.
        """
        ticket = handle.observed.get("pending_ticket")
        if not isinstance(ticket, dict):
            raise EndpointMismatch(
                "no pending operator ticket to confirm", detail={"reason": "no_pending_ticket"}
            )
        kind = ticket["kind"]
        to_zero = (kind == "teardown" and ticket["mode"] == "sleep") or (
            kind == "scale" and ticket["min_replicas"] == 0
        )
        if to_zero:
            handle.state = EndpointState.SLEEPING
        else:
            result = await self._probe(handle, self._float("status_timeout_s", 90.0))
            if kind == "teardown":
                if result.outcome not in ("not_found", "unauthorized"):
                    raise EndpointUnavailable(
                        f"deployment still answers ({result.outcome}); terminate not confirmed",
                        detail={"reason": "terminate_not_confirmed"},
                    )
                handle.state = EndpointState.RETIRED
            else:
                raise_for_probe(result, "scale confirmation")
                handle.state = EndpointState.WARM
                handle.observed["min_replicas"] = ticket["min_replicas"]
                handle.observed["max_replicas"] = ticket["max_replicas"]
        ticket["closed_at"] = self._clock.now()
        handle.observed.pop("pending_ticket", None)
        return handle.state

    # -- upstream ------------------------------------------------------------------------------

    def upstream(self, handle: EndpointHandle) -> UpstreamRoute:
        if handle.base_url is None or handle.upstream_model is None:
            raise EndpointUnavailable(f"endpoint {handle.endpoint_id} has no base_url yet")
        return UpstreamRoute(
            api_base=handle.base_url,
            model=handle.upstream_model,
            protocol=handle.protocol,
            api_key_ref=handle.credential_ref,
            cold_start_window_s=COLD_START_WINDOW_S,
        )
