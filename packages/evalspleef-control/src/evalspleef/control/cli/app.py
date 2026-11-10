"""The spleef CLI (PRD FR-OPS-01).

Commands: ``config validate``; ``models register|attach|probe|bench|warm|retire|list|tickets``;
``providers list|check|conformance``; ``hosts list|drain``; ``tasks lint``;
``batch plan|freeze|run|report``; ``gateway serve``.

Output is plain text tables; ``--json`` switches the list/report commands to JSON.
Exit codes: 0 success, 1 domain failure, 2 usage or configuration error. Secrets
(SecretRef values and resolved credentials) are never printed.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import secrets
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import typer

from evalspleef.control.batch import BatchDriver, BatchRunResult, build_freeze_record, freeze_guards
from evalspleef.control.batch.operations import OperationRefused, TrialOperations
from evalspleef.control.batch.release import ReleaseInputs, build_release
from evalspleef.control.bench import (
    API_NOTE,
    SELF_SERVED_NOTE,
    BenchPlan,
    BenchRunner,
    MonotonicClock,
    bench_id_for,
    gateway_sampler,
    hourly_price,
    parse_shape,
    raw_record,
    save,
)
from evalspleef.control.bench.report import bench_markdown, facts_line
from evalspleef.control.config import ConfigError
from evalspleef.control.endpoints import EngineLogStore, make_endpoint_id, probe_failures
from evalspleef.control.events import verify_projection
from evalspleef.control.ledger import CostLedger
from evalspleef.control.store.rows import GradeRow
from evalspleef.control.taskpack import lint_task_pack_dir
from evalspleef.core.batch import BatchGuardFailed
from evalspleef.core.models import CellStatus
from evalspleef.core.report import BatchReport, TrialResult, cell_id_for
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.model import EndpointState, HFSource, ModelSpec, TeardownMode, VendorSource

from .context import (
    AppContext,
    Settings,
    StoredEndpointStates,
    deployment_version_fields,
    ensure_resolved,
    load_models_config,
)
from .errors import EXIT_DOMAIN, EXIT_SUCCESS, EXIT_USAGE, CliError, DomainError, UsageError
from .presets import presets_app

__all__ = ["app", "main"]

app = typer.Typer(
    name="spleef",
    help="EvalSpleef control plane CLI (PRD FR-OPS-01).",
    no_args_is_help=True,
    add_completion=False,
)
config_app = typer.Typer(no_args_is_help=True)
models_app = typer.Typer(no_args_is_help=True)
providers_app = typer.Typer(no_args_is_help=True)
hosts_app = typer.Typer(no_args_is_help=True)
batch_app = typer.Typer(no_args_is_help=True)
trials_app = typer.Typer(no_args_is_help=True)
gateway_app = typer.Typer(no_args_is_help=True)
app.add_typer(config_app, name="config")
app.add_typer(models_app, name="models")
app.add_typer(providers_app, name="providers")
app.add_typer(hosts_app, name="hosts")
app.add_typer(batch_app, name="batch")
app.add_typer(trials_app, name="trials")
app.add_typer(gateway_app, name="gateway")
app.add_typer(presets_app, name="presets")


# -- shared options -------------------------------------------------------------


def _settings(
    models: Path,
    providers: Path | None,
    batch: Path | None,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
) -> Settings:
    return Settings.from_options(
        models=models,
        providers=providers,
        batch=batch,
        tasks_dir=tasks_dir,
        templates_registry=templates_registry,
        store_url=store_url,
        events_url=events_url,
        state_dir=state_dir,
    )


_MODELS_OPT = typer.Option(Path("config/models.yaml"), "--models", help="models.yaml path")
_PROVIDERS_OPT = typer.Option(
    Path("config/providers.yaml"), "--providers", help="providers.yaml path"
)
_BATCH_OPT = typer.Option(Path("config/batch.yaml"), "--batch", help="batch.yaml path")
_TASKS_OPT = typer.Option(Path("tasks"), "--tasks-dir", help="task packs root directory")
_TEMPLATES_OPT = typer.Option(
    None,
    "--templates-registry",
    help="template registry JSON (default: <tasks-dir>/../templates/registry.json)",
)
_STORE_OPT = typer.Option(None, "--store-url", envvar="SPLEEF_STORE_URL", help="result store URL")
_EVENTS_OPT = typer.Option(None, "--events-url", envvar="SPLEEF_EVENTS_URL", help="event store URL")
_STATE_OPT = typer.Option(
    None, "--state-dir", envvar="SPLEEF_STATE_DIR", help="state directory (freeze records)"
)
_JSON_OPT = typer.Option(False, "--json", help="print machine-readable JSON")
_MODEL_FILTER_OPT = typer.Option([], "--model", help="restrict the command to these slugs")
_INCLUDE_OPT = typer.Option([], "--include", help="glob, default: every pack")


# -- output helpers ---------------------------------------------------------------


def _emit_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    text = [[str(cell) for cell in row] for row in rows]
    widths = [len(str(header)) for header in headers]
    for row in text:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    line = "  ".join(str(header).ljust(widths[index]) for index, header in enumerate(headers))
    typer.echo(line)
    typer.echo("  ".join("-" * width for width in widths))
    for row in text:
        typer.echo("  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)))


def _emit_json(data: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> None:
    typer.echo(json.dumps(data, indent=2, sort_keys=True, default=str))


def _run(coro: Coroutine[Any, Any, int]) -> None:
    """Run a command coroutine and map failures to the CLI exit codes."""
    try:
        code = asyncio.run(coro)
    except CliError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(exc.exit_code) from None
    except ConfigError as exc:
        typer.secho(f"configuration error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from None
    except BatchGuardFailed as exc:
        typer.secho(f"batch guard failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_DOMAIN) from None
    except SpleefError as exc:
        typer.secho(f"error: {exc.message}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_DOMAIN) from None
    raise typer.Exit(code)


# -- config ------------------------------------------------------------------------


@config_app.command("validate")
def config_validate(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    batch: Path = _BATCH_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    as_json: bool = _JSON_OPT,
) -> None:
    """Validate models.yaml, providers.yaml and batch.yaml plus their cross-references."""
    _run(
        _config_validate(
            models,
            providers,
            batch,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            as_json,
        )
    )


async def _config_validate(
    models: Path,
    providers: Path,
    batch: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    as_json: bool,
) -> int:
    settings = _settings(
        models,
        providers,
        batch,
        tasks_dir,
        templates_registry,
        store_url,
        events_url,
        state_dir,
    )
    async with AppContext(settings) as ctx:
        specs = load_models_config(models, os.environ)
        providers_config = ctx.providers_config()
        batch_config = ctx.load_batch_config()
        # Instantiating the registry proves every referenced adapter is installed.
        registry = ctx.provider_registry()

        slugs = [spec.slug for spec in specs]
        problems: list[str] = []
        for slug in sorted({slug for slug in slugs if slugs.count(slug) > 1}):
            problems.append(f"models.yaml: duplicate slug {slug!r}")
        spec_by_slug = {spec.slug: spec for spec in specs}
        for spec in specs:
            if spec.deployment.account not in providers_config.model_accounts:
                problems.append(
                    f"models.yaml: {spec.slug}: unknown model account "
                    f"{spec.deployment.account!r} (providers.yaml model_accounts)"
                )
        for slug in batch_config.models:
            if slug not in spec_by_slug:
                problems.append(f"batch.yaml: unknown model {slug!r} (not in models.yaml)")
        for slug in batch_config.judges:
            if slug not in spec_by_slug:
                problems.append(f"batch.yaml: unknown judge {slug!r} (not in models.yaml)")
        runner_slugs = set(registry.runner_classes())
        shells = {batch_config.shells.unified, *batch_config.shells.native.values()} - {None}
        for shell in sorted(str(shell) for shell in shells):
            if shell not in runner_slugs:
                problems.append(
                    f"batch.yaml: no agent runner installed for shell {shell!r} "
                    f"(available: {sorted(runner_slugs)})"
                )
        packs = ctx.discover_task_packs(batch_config)
        if as_json:
            _emit_json(
                {
                    "ok": not problems,
                    "problems": problems,
                    "models": slugs,
                    "sandbox_instances": sorted(providers_config.sandbox_instances),
                    "model_accounts": sorted(providers_config.model_accounts),
                    "runners": sorted(runner_slugs),
                    "tasks": [pack.task_id for pack in packs],
                    "batch": batch_config.batch,
                }
            )
        else:
            for problem in problems:
                typer.secho(f"invalid: {problem}", fg=typer.colors.RED, err=True)
            typer.echo(f"models:    {len(specs)} ({', '.join(slugs)})")
            typer.echo(
                f"providers: {len(providers_config.sandbox_instances)} sandbox instance(s), "
                f"{len(providers_config.model_accounts)} model account(s)"
            )
            typer.echo(f"runners:   {', '.join(sorted(runner_slugs))}")
            typer.echo(f"tasks:     {len(packs)} pack(s): {', '.join(p.task_id for p in packs)}")
            typer.echo(f"batch:     {batch_config.batch} ({batch_config.epochs} epoch(s))")
            typer.echo("config ok" if not problems else "config invalid")
        return EXIT_SUCCESS if not problems else EXIT_USAGE


# -- models ---------------------------------------------------------------------


def _select_specs(ctx: AppContext, only: Sequence[str]) -> list[ModelSpec]:
    specs = ctx.load_model_specs()
    if not only:
        return specs
    by_slug = {spec.slug: spec for spec in specs}
    missing = [slug for slug in only if slug not in by_slug]
    if missing:
        raise UsageError(f"unknown model(s) {missing} in models.yaml")
    return [by_slug[slug] for slug in only]


def _check_account_urls(ctx: AppContext) -> None:
    """Model accounts must not carry unresolved environment placeholders."""
    config = ctx.providers_config()
    for name, account in sorted(config.model_accounts.items()):
        ensure_resolved(account.base_url, f"model account {name!r} base_url")
        ensure_resolved(
            config.gateway.internal_url if config.gateway else None,
            "providers.yaml gateway.internal_url",
        )


async def _registered_specs(ctx: AppContext, only: Sequence[str]) -> list[ModelSpec]:
    """Specs from the immutable registry (the source of truth after registration)."""
    selected = _select_specs(ctx, only)
    registered: list[ModelSpec] = []
    for spec in selected:
        stored = await ctx.model_registry.get(spec.slug, spec.spec_version)
        if stored is None:
            raise DomainError(
                f"model {spec.slug}@{spec.spec_version} is not registered; "
                f"run 'spleef models register' first"
            )
        registered.append(stored)
    return registered


@models_app.command("register")
def models_register(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    model: list[str] = _MODEL_FILTER_OPT,
) -> None:
    """Validate and register each model of models.yaml; deploy or open an operator ticket."""
    _run(
        _models_register(
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            model,
        )
    )


async def _models_register(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    model: Sequence[str],
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        _check_account_urls(ctx)
        manager = ctx.endpoint_manager()
        rejected: list[str] = []
        for spec in _select_specs(ctx, model):
            handle = await manager.register(spec)
            state = handle.state.value
            typer.echo(f"{spec.slug}@{spec.spec_version}  {handle.endpoint_id}  {state}")
            if handle.state is EndpointState.REJECTED:
                rejected.append(spec.slug)
        if rejected:
            raise DomainError(f"registration rejected: {', '.join(rejected)}")
        return EXIT_SUCCESS


@models_app.command("attach")
def models_attach(
    endpoint_id: str = typer.Option(..., "--endpoint-id"),
    base_url: str = typer.Option(..., "--base-url"),
    upstream_model: str = typer.Option(..., "--model", help="upstream model name"),
    deployment_id: str | None = typer.Option(None, "--deployment-id"),
    operator: str | None = typer.Option(None, "--operator"),
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Attach an operator-deployed endpoint (assisted mode), then probe it."""
    _run(
        _models_attach(
            endpoint_id,
            base_url,
            upstream_model,
            deployment_id,
            operator,
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
        )
    )


async def _models_attach(
    endpoint_id: str,
    base_url: str,
    upstream_model: str,
    deployment_id: str | None,
    operator: str | None,
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        manager = ctx.endpoint_manager()
        operator_input = {"base_url": base_url, "model": upstream_model}
        if deployment_id:
            operator_input["deployment_id"] = deployment_id
        if operator:
            operator_input["operator"] = operator
        handle = await manager.attach(endpoint_id, operator_input)
        report = await manager.latest_report(endpoint_id)
        failures = (
            probe_failures(report, handle.model.roles)
            if report is not None
            else ["no probe report was produced"]
        )
        typer.echo(f"{handle.endpoint_id}  {handle.state.value}")
        if failures:
            raise DomainError(f"attach probe failed for {endpoint_id}: {', '.join(failures)}")
        return EXIT_SUCCESS


@models_app.command("probe")
def models_probe(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    model: list[str] = _MODEL_FILTER_OPT,
) -> None:
    """Run the P-01..P-15 capability probe suite through the gateway for each model."""
    _run(
        _models_probe(
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            model,
        )
    )


async def _models_probe(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    model: Sequence[str],
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        _check_account_urls(ctx)
        manager = ctx.endpoint_manager()
        failed: list[str] = []
        for spec in await _registered_specs(ctx, model):
            endpoint_id = make_endpoint_id(spec)
            report = await manager.probe(endpoint_id)
            passed = sum(1 for check in report.checks if check.passed)
            failures = probe_failures(report, spec.roles)
            typer.echo(
                f"{spec.slug}  {endpoint_id}  probes {passed}/{len(report.checks)} passed"
                f"  report {report.report_id}"
            )
            for check in report.checks:
                if not check.passed:
                    typer.echo(f"  FAIL {check.probe_id}: {check.detail}")
            if failures:
                failed.append(f"{spec.slug} (required {', '.join(failures)} missing)")
        if failed:
            raise DomainError(f"probe failed: {'; '.join(failed)}")
        return EXIT_SUCCESS


_SHAPE_OPT = typer.Option(
    ["2048x128"], "--shape", help="INPUTxOUTPUT tokens of the single-request phase"
)
_SWEEP_SHAPE_OPT = typer.Option(
    [], "--sweep-shape", help="INPUTxOUTPUT tokens of the sweep (default: the first --shape)"
)


@models_app.command("bench")
def models_bench(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    model: list[str] = _MODEL_FILTER_OPT,
    shape: list[str] = _SHAPE_OPT,
    sweep_shape: list[str] = _SWEEP_SHAPE_OPT,
    repeats: int = typer.Option(3, "--repeats", min=1, help="single requests per shape"),
    concurrency: str = typer.Option(
        "1,2,4,8,16", "--concurrency", help="rising sweep levels, comma separated"
    ),
    requests_per_worker: int = typer.Option(
        4, "--requests-per-worker", min=1, help="requests each sweep worker sends back to back"
    ),
    seed: int | None = typer.Option(
        None, "--seed", help="prompt seed (random when unset; always recorded)"
    ),
    price_per_hour: float | None = typer.Option(
        None, "--price-per-hour", help="hourly price of the endpoint (default: models.yaml)"
    ),
    budget_usd: float = typer.Option(5.0, "--budget-usd", help="spend cap of the bench key"),
    timeout_s: float = typer.Option(600.0, "--timeout", help="per-request timeout in seconds"),
    ignore_eos: bool = typer.Option(
        False, "--ignore-eos", help="ask the engine to ignore EOS (self-served engines)"
    ),
    as_json: bool = _JSON_OPT,
) -> None:
    """Measure TTFT, ITL, TPOT, E2E and throughput through the gateway; store the raw samples."""
    try:
        levels = tuple(int(part) for part in concurrency.split(",") if part.strip())
        plan_template = BenchPlan(
            shapes=tuple(parse_shape(text) for text in shape),
            seed=seed if seed is not None else secrets.randbits(31),
            single_repeats=repeats,
            sweep_shapes=tuple(parse_shape(text) for text in sweep_shape),
            concurrency=levels,
            requests_per_worker=requests_per_worker,
            request_timeout_s=timeout_s,
            ignore_eos=ignore_eos,
        )
    except ValueError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(EXIT_USAGE) from None
    _run(
        _models_bench(
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            model,
            plan_template,
            price_per_hour,
            budget_usd,
            as_json,
        )
    )


_BENCHABLE = frozenset(
    {EndpointState.READY, EndpointState.WARM, EndpointState.SLEEPING, EndpointState.DEGRADED}
)
"""Endpoint states with a published gateway route."""


def _bench_facts(
    spec: ModelSpec,
    observed: Mapping[str, Any],
    gateway_version: str,
    price_per_hour: float | None,
) -> dict[str, Any]:
    """Hardware, engine and model facts of the endpoint at measurement time."""
    params = spec.deployment.params
    source = spec.source
    hf = source if isinstance(source, HFSource) else None
    return {
        "model": f"{spec.slug}@{spec.spec_version}",
        "serving": "self-served" if hf else "api",
        "note": SELF_SERVED_NOTE if hf else API_NOTE,
        "model_source": "huggingface" if hf else "vendor",
        "hf_repo": hf.repo if hf else None,
        "model_revision": hf.revision if hf else None,
        "vendor_model_id": source.model_id if isinstance(source, VendorSource) else None,
        "deployment_adapter": spec.deployment.adapter,
        **deployment_version_fields(spec, observed),
        "gpu_count": params.get("gpu_count"),
        "quantization": params.get("quantization"),
        "engine_extra_args": params.get("engine_extra_args"),
        "context_length": params.get("context_length"),
        "max_concurrency": params.get("max_concurrency"),
        "kv_pool_tokens": observed.get("kv_pool_tokens"),
        "gateway_version": gateway_version,
        "price_per_hour_usd": price_per_hour,
    }


async def _models_bench(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    model: Sequence[str],
    plan: BenchPlan,
    price_per_hour: float | None,
    budget_usd: float,
    as_json: bool,
) -> int:
    from datetime import UTC, datetime

    from evalspleef.control.probes import SystemClock

    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        _check_account_urls(ctx)
        admin = ctx.gateway_admin()
        gateway_version = await admin.version()
        documents: list[dict[str, Any]] = []
        empty: list[str] = []
        for spec in await _registered_specs(ctx, model):
            endpoint_id = make_endpoint_id(spec)
            row = await ctx.endpoints.get(endpoint_id)
            if row is None:
                raise DomainError(f"{spec.slug}: no endpoint {endpoint_id}; register it first")
            if EndpointState(row.state) not in _BENCHABLE:
                raise DomainError(
                    f"{spec.slug}: endpoint {endpoint_id} is {row.state}; the gateway route is "
                    "published when the probe passes, run 'spleef models probe' first"
                )
            price = price_per_hour if price_per_hour is not None else hourly_price(spec)
            created_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            bench_id = bench_id_for(endpoint_id, created_at, plan.seed)

            def progress(shape: Any, level: Any, slug: str = spec.slug) -> None:
                typer.echo(
                    f"{slug} {shape.label()} c={level.concurrency}: "
                    f"{level.ok}/{level.requests} ok, system decode "
                    f"{level.system_decode_tok_s or 0.0:.1f} tok/s",
                    err=True,
                )

            gateway_trial_id = f"bench-{bench_id}"
            async with gateway_sampler(
                admin,
                gateway_url=ctx.gateway_data_url(),
                endpoint_id=endpoint_id,
                trial_id=gateway_trial_id,
                plan=plan,
                budget_usd=budget_usd,
                clock=MonotonicClock(),
                wall_clock=SystemClock(),
            ) as sampler:
                samples = await BenchRunner(
                    plan, sampler, model_name=endpoint_id, on_level=progress
                ).run()
            raw = raw_record(
                bench_id=bench_id,
                endpoint_id=endpoint_id,
                model_name=endpoint_id,
                created_at=created_at,
                plan=plan,
                samples=samples,
                price_per_hour_usd=price,
                clock="time.perf_counter (client, through the gateway)",
                gateway_trial_id=gateway_trial_id,
            )
            saved = await save(
                raw,
                facts=_bench_facts(spec, row.observed, gateway_version, price),
                directory=ctx.settings.state_dir / "bench",
                repository=ctx.benchmarks,
            )
            document = saved.row.result
            documents.append(document)
            if not any(s.sample.ok for s in samples if s.phase == "single"):
                empty.append(spec.slug)
            if not as_json:
                typer.echo(f"## {spec.slug} ({endpoint_id}) benchmark {bench_id}")
                typer.echo("")
                typer.echo(facts_line(document["facts"]) + ".")
                typer.echo(str(document["facts"].get("note")) + ".")
                typer.echo("")
                for line in bench_markdown(document):
                    typer.echo(line)
            typer.echo(f"raw samples: {saved.raw_path}", err=True)
            typer.echo(f"result: {saved.result_path}", err=True)
        if as_json:
            _emit_json(documents)
        if empty:
            raise DomainError(f"no single request succeeded: {', '.join(empty)}")
        return EXIT_SUCCESS


@models_app.command("warm")
def models_warm(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    model: list[str] = _MODEL_FILTER_OPT,
) -> None:
    """Wake and health-check each endpoint until it answers through the gateway."""
    _run(
        _models_warm(
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            model,
        )
    )


async def _models_warm(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    model: Sequence[str],
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        _check_account_urls(ctx)
        manager = ctx.endpoint_manager()
        endpoint_ids = [make_endpoint_id(spec) for spec in await _registered_specs(ctx, model)]
        results = await manager.warm(endpoint_ids, 1)
        cold: list[str] = []
        for endpoint_id in endpoint_ids:
            result = results[endpoint_id]
            typer.echo(
                f"{endpoint_id}  {result.state.value}  replicas {result.replicas_required}"
                + (f"  cold_start {result.cold_start_s:.1f}s" if result.cold_start_s else "")
            )
            if result.state is not EndpointState.WARM:
                cold.append(endpoint_id)
        if cold:
            raise DomainError(f"warm failed (not warm): {', '.join(cold)}")
        return EXIT_SUCCESS


@models_app.command("retire")
def models_retire(
    policy: str = typer.Option("sleep", "--policy", help="sleep or terminate"),
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    model: list[str] = _MODEL_FILTER_OPT,
) -> None:
    """Scale down (sleep) or shut down (terminate) each endpoint after a batch."""
    if policy not in ("sleep", "terminate"):
        raise UsageError("--policy must be 'sleep' or 'terminate'")
    _run(
        _models_retire(
            policy,
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            model,
        )
    )


async def _models_retire(
    policy: str,
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    model: Sequence[str],
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        manager = ctx.endpoint_manager()
        for spec in await _registered_specs(ctx, model):
            endpoint_id = make_endpoint_id(spec)
            handle = await manager.retire(
                endpoint_id,
                policy,  # type: ignore[arg-type]
                engine_logs=EngineLogStore(ctx.settings.state_dir / "endpoint-logs", ctx.secrets),
            )
            typer.echo(f"{endpoint_id}  {handle.state.value}")
        return EXIT_SUCCESS


@models_app.command("list")
def models_list(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    as_json: bool = _JSON_OPT,
) -> None:
    """List registered model endpoints from the result store."""
    _run(
        _models_list(
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            as_json,
        )
    )


async def _models_list(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    as_json: bool,
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        rows: list[Mapping[str, Any]] = []
        for state in EndpointState:
            for row in await ctx.endpoints.list_by_state(state.value):
                rows.append(
                    {
                        "endpoint_id": row.endpoint_id,
                        "model": f"{row.model_slug}@{row.model_spec_version}",
                        "adapter": row.adapter,
                        "account": row.account,
                        "state": row.state,
                        "probe_report": row.observed.get("report_id"),
                        "created_at": row.created_at,
                        "retired_at": row.retired_at,
                    }
                )
        rows = sorted(rows, key=lambda row: str(row["endpoint_id"]))
        if as_json:
            _emit_json(rows)
        else:
            if not rows:
                typer.echo("no endpoints registered")
            _emit_table(
                ["endpoint", "model", "adapter", "account", "state", "probe report"],
                [
                    (
                        row["endpoint_id"],
                        row["model"],
                        row["adapter"],
                        row["account"],
                        row["state"],
                        row["probe_report"] or "-",
                    )
                    for row in rows
                ],
            )
        return EXIT_SUCCESS


@models_app.command("tickets")
def models_tickets(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    as_json: bool = _JSON_OPT,
    open_only: bool = typer.Option(True, "--open/--all", help="show only open tickets"),
) -> None:
    """List operator tickets (deploy/scale/teardown) of the registered endpoints."""
    _run(
        _models_tickets(
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            as_json,
            open_only,
        )
    )


async def _models_tickets(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    as_json: bool,
    open_only: bool,
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        rows: list[Mapping[str, Any]] = []
        for state in EndpointState:
            for endpoint in await ctx.endpoints.list_by_state(state.value):
                for ticket in await ctx.tickets.list_by_endpoint(endpoint.endpoint_id):
                    if open_only and ticket.status != "open":
                        continue
                    rows.append(
                        {
                            "ticket_id": ticket.ticket_id,
                            "endpoint_id": ticket.endpoint_id,
                            "kind": ticket.kind,
                            "status": ticket.status,
                            "operator": ticket.operator,
                            "opened_at": ticket.opened_at,
                        }
                    )
        if as_json:
            _emit_json(rows)
        else:
            if not rows:
                typer.echo("no tickets")
            _emit_table(
                ["ticket", "endpoint", "kind", "status", "operator", "opened"],
                [
                    (
                        row["ticket_id"],
                        row["endpoint_id"],
                        row["kind"],
                        row["status"],
                        row["operator"] or "-",
                        row["opened_at"],
                    )
                    for row in rows
                ],
            )
        return EXIT_SUCCESS


# -- providers --------------------------------------------------------------------


@providers_app.command("list")
def providers_list(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    as_json: bool = _JSON_OPT,
) -> None:
    """List discovered adapters and configured provider instances."""
    _run(
        _providers_list(
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            as_json,
        )
    )


async def _providers_list(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    as_json: bool,
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        registry = ctx.provider_registry()
        config = ctx.providers_config()
        adapters = {
            "sandbox": sorted(registry.sandbox_adapters()),
            "model": sorted(registry.model_adapters()),
            "runner": sorted(registry.runner_classes()),
        }
        instances: list[dict[str, Any]] = []
        for name in sorted(config.sandbox_instances):
            instance = config.sandbox_instances[name]
            caps = registry.capabilities(name)
            instances.append(
                {
                    "instance": name,
                    "adapter": instance.adapter,
                    "os": sorted(str(os) for os in caps.os_families),
                    "arches": sorted(str(arch) for arch in caps.arches),
                    "isolation": str(caps.isolation),
                    "net_modes": sorted(str(mode) for mode in caps.net_modes),
                    "max_concurrency": caps.max_concurrency,
                    "limits": sorted(instance.limits),
                }
            )
        accounts = [
            {
                "account": name,
                "adapter": account.adapter,
                "base_url_set": account.base_url is not None,
            }
            for name, account in sorted(config.model_accounts.items())
        ]
        if as_json:
            _emit_json(
                {"adapters": adapters, "sandbox_instances": instances, "model_accounts": accounts}
            )
        else:
            typer.echo(f"sandbox adapters: {', '.join(adapters['sandbox']) or '-'}")
            typer.echo(f"model adapters:   {', '.join(adapters['model']) or '-'}")
            typer.echo(f"agent runners:    {', '.join(adapters['runner']) or '-'}")
            if instances:
                typer.echo("")
                _emit_table(
                    ["instance", "adapter", "os", "arches", "isolation", "net modes"],
                    [
                        (
                            item["instance"],
                            item["adapter"],
                            ",".join(item["os"]),
                            ",".join(item["arches"]),
                            item["isolation"],
                            ",".join(item["net_modes"]),
                        )
                        for item in instances
                    ],
                )
            if accounts:
                typer.echo("")
                _emit_table(
                    ["account", "adapter", "base_url"],
                    [
                        (a["account"], a["adapter"], "set" if a["base_url_set"] else "-")
                        for a in accounts
                    ],
                )
        return EXIT_SUCCESS


@providers_app.command("check")
def providers_check(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Preflight every configured sandbox instance and model account."""
    _run(
        _providers_check(
            models, providers, tasks_dir, templates_registry, store_url, events_url, state_dir
        )
    )


async def _providers_check(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        registry = ctx.provider_registry()
        config = ctx.providers_config()
        failures: list[str] = []
        for name in sorted(config.sandbox_instances):
            instance = config.sandbox_instances[name]
            try:
                facts = await registry.sandbox_instance(name).preflight()
            except Exception as exc:
                failures.append(f"{name}: {exc}")
                typer.secho(f"FAIL {name} ({instance.adapter}): {exc}", fg=typer.colors.RED)
                continue
            typer.echo(
                f"ok   {name} ({instance.adapter}) "
                f"server={facts.get('server_version')} arch={facts.get('host_arch')}"
            )
        for name in sorted(config.model_accounts):
            account = config.model_accounts[name]
            provider = registry.model_provider(name)
            typer.echo(
                f"ok   account {name} (adapter {account.adapter}, "
                f"management {provider.management_mode.value})"
            )
        if failures:
            raise DomainError(f"provider check failed: {'; '.join(failures)}")
        return EXIT_SUCCESS


@providers_app.command("conformance")
def providers_conformance(
    ctx: typer.Context,
    provider: str = typer.Option(..., "--provider", help="conformance target name"),
) -> None:
    """Run the conformance kit for one provider (wraps the pytest plugin)."""
    args = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "evalspleef.conformance.plugin",
        "--spleef-provider",
        provider,
        *ctx.args,
    ]
    completed = subprocess.run(args, check=False)
    if completed.returncode == 0:
        raise typer.Exit(EXIT_SUCCESS)
    raise typer.Exit(EXIT_DOMAIN)


# -- hosts ------------------------------------------------------------------------


@hosts_app.command("list")
def hosts_list(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    as_json: bool = _JSON_OPT,
) -> None:
    """List hosts reported by hostd agents."""
    _run(
        _hosts_list(
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            as_json,
        )
    )


async def _hosts_list(
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    as_json: bool,
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        rows = await ctx.hosts.list_all()
        if as_json:
            _emit_json(
                [
                    {
                        "host_id": row.host_id,
                        "instance": row.instance,
                        "state": row.state,
                        "last_heartbeat": row.last_heartbeat,
                    }
                    for row in rows
                ]
            )
        else:
            if not rows:
                typer.echo("no hosts registered (hostd agents report heartbeats here)")
            _emit_table(
                ["host", "instance", "state", "last heartbeat"],
                [(row.host_id, row.instance, row.state, row.last_heartbeat or "-") for row in rows],
            )
        return EXIT_SUCCESS


@hosts_app.command("drain")
def hosts_drain(
    host_id: str = typer.Option(..., "--host-id"),
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Mark a host draining: its trials finish, new leases go elsewhere."""
    _run(
        _hosts_drain(
            host_id,
            models,
            providers,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
        )
    )


async def _hosts_drain(
    host_id: str,
    models: Path,
    providers: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
) -> int:
    settings = _settings(
        models, providers, None, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        row = await ctx.hosts.get(host_id)
        if row is None:
            raise DomainError(f"no host {host_id!r}")
        await ctx.hosts.update_state(host_id, "draining")
        typer.echo(f"{host_id}  {row.state} -> draining")
        return EXIT_SUCCESS


# -- tasks ------------------------------------------------------------------------


@app.command("lint")
def tasks_lint(
    tasks_dir: Path = _TASKS_OPT,
    include: list[str] = _INCLUDE_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Lint task packs against the pack contract (PRD appendix E.1)."""
    del store_url, events_url, state_dir  # linting needs no stores
    _run(_tasks_lint(tasks_dir, include))


async def _tasks_lint(tasks_dir: Path, include: Sequence[str]) -> int:
    failed = await asyncio.to_thread(_lint_packs_on_disk, tasks_dir, include)
    if failed:
        raise DomainError("task pack lint failed")
    return EXIT_SUCCESS


def _lint_packs_on_disk(tasks_dir: Path, include: Sequence[str]) -> bool:
    """Lint every matching pack directory; True when at least one pack has errors."""
    if not tasks_dir.is_dir():
        raise UsageError(f"tasks directory not found: {tasks_dir}")
    patterns = list(include) or ["*"]
    failed = False
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if not any(fnmatch.fnmatchcase(child.name, pattern) for pattern in patterns):
            continue
        report = lint_task_pack_dir(child)
        if report.ok:
            typer.echo(f"ok   {report.task_id}")
        else:
            failed = True
            for issue in report.issues:
                color = typer.colors.RED if issue.severity == "error" else typer.colors.YELLOW
                typer.secho(f"{issue.severity}  {report.task_id}: {issue}", fg=color, err=True)
    return failed


# -- batch ------------------------------------------------------------------------


def _plan_row(cell: Any) -> tuple[str, str, str, str, str]:
    status = cell.skip_reason or "ok"
    return (
        cell.cell_id,
        cell.task.task_id,
        f"{cell.model.slug}@{cell.model.spec_version}",
        cell.model.shell,
        f"{status} ({len(cell.trials)} trial(s))",
    )


@batch_app.command("plan")
def batch_plan(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    batch: Path = _BATCH_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    as_json: bool = _JSON_OPT,
) -> None:
    """Plan the batch: cells, skips with reasons, and the worst-case budget."""
    _run(
        _batch_plan(
            models,
            providers,
            batch,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            as_json,
        )
    )


async def _batch_plan(
    models: Path,
    providers: Path,
    batch: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    as_json: bool,
) -> int:
    settings = _settings(
        models, providers, batch, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        batch_config = ctx.load_batch_config()
        plan = await ctx.build_plan(batch_config)
        if as_json:
            _emit_json(
                {
                    "batch_id": plan.batch_id,
                    "epochs": plan.epochs,
                    "cells": [
                        {
                            "cell_id": cell.cell_id,
                            "skipped": cell.skipped,
                            "skip_reason": cell.skip_reason,
                            "trials": len(cell.trials),
                        }
                        for cell in plan.cells
                    ],
                    "budget": {
                        "total_usd": plan.budget.total_usd,
                        "cap_usd": plan.budget.cap_usd,
                        "within_cap": plan.budget.within_cap,
                    },
                }
            )
        else:
            for cell in plan.cells:
                typer.echo("\t".join(_plan_row(cell)))
            cap = f"{plan.budget.cap_usd:.2f}" if plan.budget.cap_usd is not None else "none"
            typer.echo(
                f"cells: {len(plan.cells)} runnable, {len(plan.skipped_cells)} skipped; "
                f"worst-case spend {plan.budget.total_usd:.2f} USD (cap {cap} USD)"
            )
        return EXIT_SUCCESS


@batch_app.command("freeze")
def batch_freeze(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    batch: Path = _BATCH_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Check the FR-FLOW-01 freeze guards and store the version matrix record."""
    _run(
        _batch_freeze(
            models,
            providers,
            batch,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
        )
    )


async def _batch_freeze(
    models: Path,
    providers: Path,
    batch: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
) -> int:
    settings = _settings(
        models, providers, batch, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        batch_config = ctx.load_batch_config()
        await _require_prices(ctx, batch_config)
        plan = await ctx.build_plan(batch_config)
        lint_ok = _lint_packs(ctx, batch_config)
        freeze = await _freeze_record(ctx, plan, batch_config)
        guards = freeze_guards(
            plan, freeze, lint_passed=lint_ok, budget_approved=plan.budget.within_cap
        )
        problems = [str(reason) for reason in guards.reasons.values()]
        if problems or not lint_ok:
            for problem in problems:
                typer.secho(f"freeze guard failed: {problem}", fg=typer.colors.RED, err=True)
            raise DomainError("batch freeze guards are not satisfied")
        path = ctx.store_freeze(freeze.to_dict())
        typer.echo(f"batch {plan.batch_id} frozen")
        typer.echo(f"version matrix hash: {freeze.version_matrix_hash}")
        typer.echo(f"freeze record: {path}")
        return EXIT_SUCCESS


def _lint_packs(ctx: AppContext, batch_config: Any) -> bool:
    """Lint every pack of the batch; print issues, return the aggregate verdict."""
    ok = True
    for pack in ctx.discover_task_packs(batch_config):
        report = lint_task_pack_dir(pack.root)
        for issue in report.issues:
            if issue.severity == "error":
                ok = False
                typer.secho(f"error  {report.task_id}: {issue}", fg=typer.colors.RED, err=True)
    return ok


async def _require_prices(ctx: AppContext, batch_config: Any) -> None:
    """Refuse a batch whose models carry no explicit price: its cost cap could never trigger.

    A route without ``deployment.params.pricing`` is costed at zero, so every spend check passes.
    A free local model states that with zero prices.
    """
    unpriced = []
    for slug in (*batch_config.models, *batch_config.judges):
        spec = await ctx.model_registry.resolve(slug)
        if not isinstance(spec.deployment.params.get("pricing"), Mapping):
            unpriced.append(slug)
    if unpriced:
        raise DomainError(
            f"no deployment.params.pricing for {', '.join(unpriced)}: the batch cost cap cannot "
            "be enforced; give per-Mtok prices (0 for a free local model)"
        )


async def _freeze_record(ctx: AppContext, plan: Any, batch_config: Any) -> Any:
    """The freeze record of a plan: versions, digests and profiles of its inputs."""
    from datetime import UTC, datetime

    registry = ctx.provider_registry()
    runner_classes = ctx.runner_classes()
    shells = {cell.model.shell for cell in plan.runnable_cells}
    try:
        gateway_version = await ctx.gateway_admin().version()
    except Exception:
        gateway_version = "unavailable"
    instances = sorted(
        {
            instance
            for cell in plan.cells
            for instance in (*cell.match.agent.candidates, *cell.match.verifier.candidates)
        }
    )
    real_profiles = {instance: registry.isolation_profile(instance) for instance in instances}
    # Host, runtime and guest of every instance the batch may use, from the context's
    # one-per-run snapshot that the trials of this run record as well.
    environments = {instance: await ctx.environments().get(instance) for instance in instances}
    digests = {
        cell.task.template.ref: {instance: cell.task.template.digest for instance in instances}
        for cell in plan.runnable_cells
    }
    judges: dict[str, dict[str, Any]] = {}
    for slug in batch_config.judges:
        spec = await ctx.model_registry.resolve(slug)
        source = spec.source
        judges[slug] = {
            "spec_version": spec.spec_version,
            "endpoint_id": make_endpoint_id(spec),
            "family": spec.family,
            "revision": getattr(source, "revision", None),
        }
    return build_freeze_record(
        plan,
        gateway_version=gateway_version,
        price_table_version=batch_config.pricing_table,
        runner_versions={
            shell: getattr(runner_classes.get(shell), "version", "unknown") for shell in shells
        },
        template_digests=digests,
        isolation_profiles=real_profiles,
        judges=judges,
        frozen_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        provider_environments=environments,
    )


@batch_app.command("run")
def batch_run(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    batch: Path = _BATCH_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Run a frozen batch end to end and print the aggregate report.

    Exits 1 after printing the report when any runnable cell ended Pending, that is
    every trial of it ended failed_infra or invalid. Scored cells, cells in the
    manual-review queue and skipped cells do not fail the run.
    """
    _run(
        _batch_run(
            models,
            providers,
            batch,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
        )
    )


async def _batch_run(
    models: Path,
    providers: Path,
    batch: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
) -> int:
    settings = _settings(
        models, providers, batch, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        _check_account_urls(ctx)
        batch_config = ctx.load_batch_config()
        await _require_prices(ctx, batch_config)
        plan = await ctx.build_plan(batch_config)
        gateway_url = ctx.gateway_data_url()
        lint_ok = _lint_packs(ctx, batch_config)
        freeze = await _freeze_record(ctx, plan, batch_config)
        scheduler = await ctx.build_scheduler()
        for warning in ctx.preflight_warnings:
            typer.secho(f"warning: {warning}", fg=typer.colors.YELLOW, err=True)
        if ctx.preflight_bridges:
            typer.secho(
                "warning: S5 isolation probes waived for "
                + ", ".join(ctx.preflight_bridges)
                + " (the adapters ship prose probe descriptions; see the report)",
                fg=typer.colors.YELLOW,
                err=True,
            )
        runner = ctx.make_trial_runner(
            gateway_url=gateway_url,
            judges=await ctx.build_judges(batch_config, gateway_url),
            target_family=(await ctx.model_registry.resolve(batch_config.models[0])).family,
            versions_by_endpoint=await ctx.trial_version_seeds(plan, freeze),
            allow_single_judge_vendor=batch_config.allow_single_judge_vendor,
            agent_judge=await ctx.build_agent_judge(batch_config, plan, gateway_url),
        )
        judges_note = ", ".join(sorted(batch_config.judges))
        if judges_note:
            typer.echo(f"judge panel: {judges_note}")
        registry = ctx.provider_registry()
        profiles = {name: registry.isolation_profile(name) for name in registry.sandbox_instances()}
        driver = BatchDriver(
            plan=plan,
            freeze=freeze,
            executor=runner,
            scheduler=scheduler,
            leases=ctx.lease_manager(),
            trials=ctx.trials,
            grades=ctx.grades,
            events=ctx.events,
            endpoint_states=StoredEndpointStates(ctx.endpoints),
            isolation_profiles=profiles,
            ledger=CostLedger(ctx.db),
        )
        endpoint_ids = sorted({cell.model.endpoint_id for cell in plan.runnable_cells})
        manager = ctx.endpoint_manager()
        # The freeze guards run before any endpoint is warmed or retired: a batch that may
        # not start leaves its endpoints as they were, so it can be fixed and rerun.
        driver.freeze(lint_passed=lint_ok, budget_approved=plan.budget.within_cap)
        if batch_config.endpoint_policy.warm_before_run:
            warm = await manager.warm(endpoint_ids, 1)
            for endpoint_id, warm_result in sorted(warm.items()):
                typer.echo(f"warm {endpoint_id}: {warm_result.state.value}")
        run_result: BatchRunResult | None = None
        try:
            run_result = await driver.run()
        finally:
            if batch_config.endpoint_policy.retire_after:
                policy = cast("TeardownMode", batch_config.endpoint_policy.retire_after)
                for endpoint_id in endpoint_ids:
                    try:
                        handle = await manager.retire(
                            endpoint_id,
                            policy,
                            engine_logs=EngineLogStore(
                                ctx.settings.state_dir / "endpoint-logs", ctx.secrets
                            ),
                        )
                        typer.echo(f"retire {endpoint_id}: {handle.state.value}")
                    except SpleefError as exc:
                        typer.secho(
                            f"warning: retire {endpoint_id} failed: {exc.message}",
                            fg=typer.colors.YELLOW,
                            err=True,
                        )
        ctx.store_freeze(freeze.to_dict())
        assert run_result is not None  # set by driver.run() above
        typer.echo("")
        typer.echo(run_result.report.to_markdown())
        for alert in run_result.alerts:
            typer.secho(f"scheduler alert: {alert}", fg=typer.colors.YELLOW, err=True)
        from datetime import UTC, datetime

        from evalspleef.control.batch.mirrors import verify_mirrors

        frozen_specs = [await ctx.model_registry.resolve(slug) for slug in batch_config.models]
        moved = await verify_mirrors(
            frozen_specs,
            ctx.hf,
            ctx.events,
            batch_id=plan.batch_id,
            ts=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        for slug in moved:
            typer.secho(
                f"incident: the HF mirror of {slug} no longer resolves to its frozen commit",
                fg=typer.colors.RED,
                err=True,
            )
        # Trial rows are projections of the event log; a row the log does not derive is a bug.
        projection = await verify_projection(ctx.events, ctx.db, batch_id=plan.batch_id)
        typer.echo(f"event projection: {projection.checked} trial row(s) checked")
        if not projection.ok:
            for mismatch in projection.mismatches:
                typer.secho(
                    f"projection mismatch {mismatch.trial_id}: row {mismatch.db_state}, "
                    f"events {mismatch.event_state} {mismatch.error or ''}".rstrip(),
                    fg=typer.colors.RED,
                    err=True,
                )
        unfinished = _pending_cells_error(run_result.report)
        if unfinished is not None:
            raise DomainError(unfinished)
        return EXIT_DOMAIN if moved or not projection.ok else EXIT_SUCCESS


def _pending_cells_error(report: BatchReport) -> str | None:
    """The failure line of a run that left runnable cells without a result, else None.

    A Pending cell has no counted trial and none waiting for a human, so every trial of
    it ended failed_infra or invalid. Scored, Manual (the review queue) and Skip cells
    are results.
    """
    cells = report.build_cells()
    pending = {cell.cell_id for cell in cells if cell.status == CellStatus.PENDING}
    if not pending:
        return None
    runnable = sum(1 for cell in cells if cell.status != CellStatus.SKIP)
    states = Counter(
        str(trial.state)
        for trial in report.trials
        if cell_id_for(trial.task_id, trial.model, trial.shell) in pending
    )
    trials = ", ".join(f"{count} {state}" for state, count in sorted(states.items()))
    return (
        f"{len(pending)} of {runnable} runnable cell(s) ended Pending: their trials ended "
        f"failed_infra or invalid ({trials}); see `spleef trials list --batch-id "
        f"{report.batch_id}` for the reasons"
    )


_RELEASE_OPT = typer.Option(
    None, "--release", help="also write the release package (drill-down, manifests) here"
)
_NOVITA_BILL_OPT = typer.Option(
    None, "--novita-bill", help="Novita billing JSON to import before reconciling"
)


@batch_app.command("costs")
def batch_costs(
    batch_id: str = typer.Option(..., "--batch-id"),
    novita_bill: Path | None = _NOVITA_BILL_OPT,
    period: str = typer.Option("", "--period", help="billing period of that bill, e.g. 2026-11"),
    tolerance: float = typer.Option(0.05, "--tolerance"),
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    as_json: bool = _JSON_OPT,
) -> None:
    """Show the batch cost ledger by kind and reconcile it with the vendor bill (FR-OPS-04)."""
    bill_text = None if novita_bill is None else novita_bill.read_text(encoding="utf-8")

    async def run() -> int:
        async with AppContext(_store_settings(store_url, events_url, state_dir)) as ctx:
            ledger = CostLedger(ctx.db)
            if bill_text is not None:
                try:
                    imported = await ledger.import_novita_billing(
                        bill_text,
                        batch_id=batch_id,
                        period=period or batch_id[:7],
                    )
                except ValueError as exc:
                    raise DomainError(f"bill import failed: {exc}") from exc
                typer.echo(f"imported {len(imported)} bill line(s)", err=True)
            report = await ledger.reconcile(batch_id, tolerance=tolerance)
            rows = [
                {
                    "kind": str(kind.kind),
                    "ledger_usd": round(kind.ledger_usd, 6),
                    "bill_usd": round(kind.bill_usd, 6),
                    "relative_difference": kind.relative_difference,
                    "ok": kind.ok,
                }
                for kind in report.kinds
            ]
            if as_json:
                payload = {"batch_id": batch_id, "tolerance": tolerance, "ok": report.ok}
                typer.echo(json.dumps({**payload, "kinds": rows}, indent=2))
            else:
                for row in rows:
                    verdict = "ok" if row["ok"] else "MISMATCH"
                    typer.echo(
                        f"{row['kind']:<8} ledger {row['ledger_usd']:.4f} USD  "
                        f"bill {row['bill_usd']:.4f} USD  {verdict}"
                    )
            return EXIT_SUCCESS if report.ok else EXIT_DOMAIN

    _run(run())


@batch_app.command("report")
def batch_report(
    models: Path = _MODELS_OPT,
    providers: Path = _PROVIDERS_OPT,
    batch: Path = _BATCH_OPT,
    tasks_dir: Path = _TASKS_OPT,
    templates_registry: Path | None = _TEMPLATES_OPT,
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
    as_json: bool = _JSON_OPT,
    release: Path | None = _RELEASE_OPT,
) -> None:
    """Aggregate the stored trial results into the batch report."""
    _run(
        _batch_report(
            models,
            providers,
            batch,
            tasks_dir,
            templates_registry,
            store_url,
            events_url,
            state_dir,
            as_json,
            release,
        )
    )


def _write_release(directory: Path, files: Mapping[str, str]) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, content in sorted(files.items()):
        path = directory / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written


async def _batch_report(
    models: Path,
    providers: Path,
    batch: Path,
    tasks_dir: Path,
    templates_registry: Path | None,
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    as_json: bool,
    release: Path | None = None,
) -> int:
    from datetime import UTC, datetime

    settings = _settings(
        models, providers, batch, tasks_dir, templates_registry, store_url, events_url, state_dir
    )
    async with AppContext(settings) as ctx:
        batch_config = ctx.load_batch_config()
        batch_id = batch_config.batch
        results: list[TrialResult] = []
        trial_rows = await ctx.trials.list_by_batch(batch_id)
        grades_by_trial: dict[str, list[GradeRow]] = {}
        for row in trial_rows:
            grade_rows = await ctx.grades.list_by_trial(row.trial_id)
            grades_by_trial[row.trial_id] = grade_rows
            record: Mapping[str, Any] = {
                "trial_id": row.trial_id,
                "state": row.state,
                "validity": row.validity,
                "grades": [dict(grade.payload) for grade in grade_rows],
                "extensions": dict(row.extensions or {}),
            }
            results.append(TrialResult.from_record(record))
        try:
            plan = await ctx.build_plan(batch_config)
            skips = plan.skips()
        except (CliError, SpleefError):
            skips = {}
        report = BatchReport(
            batch_id=batch_id,
            trials=tuple(results),
            skips=skips,
            generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        if as_json:
            _emit_json(report.to_dict())
        else:
            typer.echo(report.to_markdown())
        if release is not None:
            freeze = ctx.load_stored_freeze(batch_id)
            probe_rows = {}
            for report_id in ((freeze or {}).get("probe_report_ids") or {}).values():
                probe_row = await ctx.probe_reports.get(str(report_id))
                if probe_row is not None:
                    probe_rows[str(report_id)] = probe_row
            benchmarks = {}
            for endpoint_id in sorted(map(str, (freeze or {}).get("probe_report_ids") or {})):
                benchmark = await ctx.benchmarks.latest(endpoint_id)
                if benchmark is not None:
                    benchmarks[endpoint_id] = benchmark
            specs = {}
            for slug in sorted({row.model for row in trial_rows}):
                spec = await ctx.model_registry.get_latest(slug)
                if spec is not None:
                    specs[slug] = spec
            files = build_release(
                ReleaseInputs(
                    report=report,
                    trials=trial_rows,
                    grades=grades_by_trial,
                    events=await ctx.events.read(batch_id),
                    freeze=freeze,
                    probe_reports=probe_rows,
                    models=specs,
                    judges=list(batch_config.judges),
                    costs=await CostLedger(ctx.db).reconcile(batch_id),
                    benchmarks=benchmarks,
                )
            )
            for path in _write_release(release, files):
                typer.echo(f"release: {path}", err=True)
        return EXIT_SUCCESS


# -- gateway ----------------------------------------------------------------------


@gateway_app.command(
    "serve",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def gateway_serve(ctx: typer.Context) -> None:
    """Run the model gateway (delegates to the spleef-gateway console script)."""
    if shutil.which("spleef-gateway") is None:
        typer.secho(
            "error: the 'spleef-gateway' executable is not on PATH",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)
    os.execvpe("spleef-gateway", ["spleef-gateway", *ctx.args], os.environ)


# -- trials -----------------------------------------------------------------------


def _store_settings(
    store_url: str | None, events_url: str | None, state_dir: Path | None
) -> Settings:
    return _settings(
        Path("config/models.yaml"),
        None,
        None,
        Path("tasks"),
        None,
        store_url,
        events_url,
        state_dir,
    )


async def _trial_operation(
    store_url: str | None,
    events_url: str | None,
    state_dir: Path | None,
    action: Callable[[TrialOperations], Awaitable[str]],
) -> int:
    from datetime import UTC, datetime

    async with AppContext(_store_settings(store_url, events_url, state_dir)) as ctx:
        operations = TrialOperations(
            events=ctx.events,
            trials=ctx.trials,
            grades=ctx.grades,
            now=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        try:
            typer.echo(await action(operations))
        except OperationRefused as exc:
            raise DomainError(str(exc)) from exc
        return EXIT_SUCCESS


@trials_app.command("list")
def trials_list(
    batch_id: str = typer.Option(..., "--batch-id"),
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """List the trials of a batch with state, validity and the latest grade."""

    async def run() -> int:
        async with AppContext(_store_settings(store_url, events_url, state_dir)) as ctx:
            for row in await ctx.trials.list_by_batch(batch_id):
                grades = await ctx.grades.list_by_trial(row.trial_id)
                latest = (
                    f"v{grades[-1].version} {grades[-1].tier} ({grades[-1].decided_by})"
                    if grades
                    else "-"
                )
                typer.echo(f"{row.trial_id}\t{row.state}\t{row.validity}\t{latest}")
            return EXIT_SUCCESS

    _run(run())


@trials_app.command("regrade")
def trials_regrade(
    trial_id: str = typer.Option(..., "--trial-id"),
    reason: str = typer.Option(..., "--reason"),
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Re-run grading on the recorded inputs and append a grade version (appendix B.6)."""

    async def action(ops: TrialOperations) -> str:
        row = await ops.regrade(trial_id, reason=reason)
        return f"{trial_id}  grade v{row.version}: {row.tier} ({row.rule_id})"

    _run(_trial_operation(store_url, events_url, state_dir, action))


@trials_app.command("void")
def trials_void(
    trial_id: str = typer.Option(..., "--trial-id"),
    reason: str = typer.Option(..., "--reason"),
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Invalidate a trial along the legal path; it leaves the leaderboard (appendix B.6)."""

    async def action(ops: TrialOperations) -> str:
        state = await ops.void(trial_id, reason=reason)
        return f"{trial_id}  -> {state}"

    _run(_trial_operation(store_url, events_url, state_dir, action))


@trials_app.command("expire-paused")
def trials_expire_paused(
    batch_id: str = typer.Option(..., "--batch-id"),
    max_pause_s: int = typer.Option(1800, "--max-pause-s", min=1),
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Invalidate trials paused for a human longer than the window (P5, 30 min by default)."""

    async def action(ops: TrialOperations) -> str:
        expired = await ops.expire_paused(batch_id, max_pause_s=max_pause_s)
        return "\n".join(f"{trial_id}  -> invalid (P5)" for trial_id in expired) or "none expired"

    _run(_trial_operation(store_url, events_url, state_dir, action))


@trials_app.command("resolve")
def trials_resolve(
    trial_id: str = typer.Option(..., "--trial-id"),
    tier: str = typer.Option(..., "--tier", help="P, A, B+, B, C+, C, D or F"),
    penalty: int = typer.Option(..., "--penalty"),
    reviewer: str = typer.Option(..., "--reviewer"),
    reason: str = typer.Option(..., "--reason"),
    store_url: str | None = _STORE_OPT,
    events_url: str | None = _EVENTS_OPT,
    state_dir: Path | None = _STATE_OPT,
) -> None:
    """Record a human decision for a paused trial, or override a scored one (appendix B.7)."""

    async def action(ops: TrialOperations) -> str:
        row = await ops.resolve(
            trial_id, tier=tier, penalty=penalty, reviewer=reviewer, reason=reason
        )
        return f"{trial_id}  grade v{row.version}: {row.tier} (human, {reviewer})"

    _run(_trial_operation(store_url, events_url, state_dir, action))


# -- entry point ------------------------------------------------------------------


def main() -> None:
    app()


if __name__ == "__main__":
    main()
