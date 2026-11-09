"""PRD AC-03: the same Linux task packs on two real carriers, docker and apple-container.

One batch definition (the example batch.yaml, verbatim: 3 packs x 2 fake models x 2
epochs) runs through the real ``spleef`` CLI twice, each time with its own gateway
process, result store and event log. Only providers.yaml differs: the first run routes
Linux sandboxes to the local Docker engine, the second to Apple's ``container`` runtime
(one lightweight VM per sandbox). Both runs boot the same pack images, pinned by the same
OCI index digest: each Docker image is exported as an OCI archive and loaded into the
Apple image store, and one generated template registry serves both runs.

The scenario asserts that every pack grades the same on both carriers (tier, decision
rule, penalty and its breakdown), that each trial row's carrier and recorded execution
environment belong to the carrier that ran it (checked against facts read straight from
the docker and container CLIs and from a VM booted outside the platform), and that the
host facts are the same on both.

Opt-in: it needs ``-m e2e``, ``SPLEEF_E2E_APPLE_CONTAINER=1`` and Apple's ``container``
CLI with a running service, so a plain ``pytest -m e2e`` elsewhere is unchanged. With
the opt-in set, a missing CLI fails the test instead of skipping it.
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

from evalspleef.core.models import parse_trial_id
from evalspleef.core.state import IllegalTransition, TrialMachine

REPO = Path(__file__).resolve().parents[2]
TASK_IDS = ("logic-001", "data-001", "debug-001")
AC03_REPORTS = REPO / ".omo" / "reports" / "e2e-ac03"
BATCH_ID = "2026-11-b1"
REF_MODEL = "fake-ref-agent"
EMPTY_MODEL = "fake-empty-agent"
EMPTY_RULE_IDS = {"G-BUDGET-F", "G-ABORT-04"}
EPOCHS = 2  # docs/03 9.9: a cell needs >= 2 valid epochs to be ranked
TRIALS = len(TASK_IDS) * 2 * EPOCHS
HOST_OS_FAMILY = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}
# Every image, container and tag this scenario creates carries this prefix.
PREFIX = "spleef-ac03-"
DOCKER_INSTANCE = "local-docker"
APPLE_INSTANCE = "local-apple-container"
APPLE_CONCURRENCY = 3  # VMs of 2 GiB each (pack requirement), agent and verifier per trial
OPT_IN = os.environ.get("SPLEEF_E2E_APPLE_CONTAINER") == "1"

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not OPT_IN,
        reason="the docker/apple-container parity run is opt-in: set "
        "SPLEEF_E2E_APPLE_CONTAINER=1 on a Mac with Apple's container CLI",
    ),
]


def _run(argv: list[str], *, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _checked(argv: list[str], *, timeout: float = 120) -> str:
    result = _run(argv, timeout=timeout)
    if result.returncode != 0:
        pytest.fail(
            f"{' '.join(argv)} exited {result.returncode}:\n{result.stderr.strip()[-1500:]}"
        )
    return result.stdout.strip()


# -- the Apple container CLI ---------------------------------------------------------


@pytest.fixture(scope="module")
def container_cli() -> str:
    """The ``container`` executable and a reachable service; a hard failure once opted in."""
    executable = shutil.which("container")
    if executable is None:
        pytest.fail(
            "SPLEEF_E2E_APPLE_CONTAINER=1 is set but Apple's 'container' CLI is not on PATH"
        )
    listing = _run([executable, "list", "--all", "--format", "json"], timeout=60)
    if listing.returncode != 0:
        pytest.fail(
            "the container service is not reachable (start it with 'container system "
            f"start'): {listing.stderr.strip()[:300]}"
        )
    return executable


def _container_ids(cli: str) -> set[str]:
    listing = json.loads(_checked([cli, "list", "--all", "--format", "json"]))
    return {str((entry.get("configuration") or {}).get("id")) for entry in listing}


# -- pack images, pinned once and loaded into both image stores ----------------------


@dataclass(frozen=True)
class PinnedImage:
    task_id: str
    source_tag: str  # the Docker tag scripts/build_templates.py produced
    tag: str  # the tag both carriers boot
    docker_id: str  # Docker's image id: the OCI index digest (containerd image store)
    apple_digest: str  # the index digest the Apple image store reports after loading


@pytest.fixture(scope="module")
def pinned_images(
    docker_ready: None, container_cli: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[dict[str, PinnedImage]]:
    """Each pack image under a ``spleef-ac03-`` tag in Docker and in the Apple store.

    The Docker image is exported as an OCI archive with its full index and loaded into
    the Apple store, so both stores hold the same index digest. The Apple store resolves
    ``ref@digest`` only through a stored digest reference, which a loaded archive does
    not create, so the digest reference is tagged explicitly.
    """
    del docker_ready
    archives = tmp_path_factory.mktemp("ac03-images")
    pinned: dict[str, PinnedImage] = {}
    try:
        for task_id in TASK_IDS:
            source = f"llm-eval/{task_id}:local"
            tag = f"{PREFIX}{task_id}:local"
            _run([container_cli, "image", "delete", "--force", tag])  # a crashed run's leftover
            docker_id = _checked(["docker", "image", "inspect", source, "--format", "{{.Id}}"])
            _checked(["docker", "tag", docker_id, tag])
            archive = archives / f"{task_id}.tar"
            _checked(["docker", "save", "--output", str(archive), tag], timeout=600)
            _checked([container_cli, "image", "load", "--input", str(archive)], timeout=600)
            archive.unlink()
            inspected = json.loads(_checked([container_cli, "image", "inspect", tag]))
            apple_digest = str(inspected[0]["configuration"]["descriptor"]["digest"])
            repository = tag.rsplit(":", 1)[0]
            _checked([container_cli, "image", "tag", tag, f"{repository}@{apple_digest}"])
            pinned[task_id] = PinnedImage(task_id, source, tag, docker_id, apple_digest)
        yield pinned
    finally:
        for task_id in TASK_IDS:
            tag = f"{PREFIX}{task_id}:local"
            image = pinned.get(task_id)
            refs = [tag]
            if image is not None:
                refs.append(f"{tag.rsplit(':', 1)[0]}@{image.apple_digest}")
            _run([container_cli, "image", "delete", "--force", *refs])
            _run(["docker", "image", "rm", tag])


@pytest.fixture(scope="module")
def parity_registry(
    pinned_images: Mapping[str, PinnedImage], tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """One template registry for both runs: the pinned tag and digest of every pack."""
    path = tmp_path_factory.mktemp("ac03-registry") / "registry.json"
    registry = {
        image.task_id: {
            "tag": image.tag,
            "image_id": image.docker_id,
            "source_tag": image.source_tag,
        }
        for image in pinned_images.values()
    }
    path.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def apple_vm_kernel(container_cli: str, pinned_images: Mapping[str, PinnedImage]) -> str:
    """The guest kernel release of a VM booted from a pack image outside the platform."""
    image = pinned_images[TASK_IDS[0]]
    return _checked(
        [
            container_cli,
            "run",
            "--rm",
            "--name",
            f"{PREFIX}kernel-{secrets.token_hex(4)}",
            "--network",
            "none",
            "--user",
            "agent",
            f"{image.tag}@{image.apple_digest}",
            "uname",
            "-r",
        ],
        timeout=180,
    )


# -- carriers: the only thing that differs between the two runs ----------------------


@dataclass(frozen=True)
class Carrier:
    adapter: str  # sandbox provider entry point
    instance: str  # providers.yaml sandbox instance the routes prefer
    declared_isolation: str
    runtime: Mapping[str, str]  # expected provider runtime, from the carrier's own CLI
    guest_kernel: str  # expected guest kernel, read outside the platform


def _docker_fact(command: str, template: str) -> str:
    return _checked(["docker", command, "--format", template], timeout=30)


def _docker_carrier() -> Carrier:
    return Carrier(
        adapter="docker",
        instance=DOCKER_INSTANCE,
        declared_isolation="L1",
        runtime={"name": "docker", "version": _docker_fact("version", "{{.Server.Version}}")},
        guest_kernel=_docker_fact("info", "{{.KernelVersion}}"),
    )


def _apple_carrier(cli: str, vm_kernel: str) -> Carrier:
    banner = _checked([cli, "--version"])
    match = re.search(r"version (\S+)", banner)
    assert match is not None, f"unexpected 'container --version' output: {banner!r}"
    return Carrier(
        adapter="apple-container",
        instance=APPLE_INSTANCE,
        declared_isolation="L3",
        runtime={"name": "container", "version": match.group(1)},
        guest_kernel=vm_kernel,
    )


def _apple_providers_yaml(example: str) -> str:
    """The example providers.yaml with Linux sandboxes routed to the Apple carrier."""
    data = yaml.safe_load(example)
    data["sandbox_instances"] = {
        APPLE_INSTANCE: {
            "adapter": "apple-container",
            # The container name is also the guest hostname; see docs/evidence/ac03-carriers.md.
            "config": {"name_prefix": PREFIX},
            "limits": {"max_concurrency": APPLE_CONCURRENCY},
        }
    }
    data["routes"] = [
        {"when": {"os": "linux"}, "prefer": [APPLE_INSTANCE]},
        {"when": {"role": "verifier"}, "prefer": [APPLE_INSTANCE]},
    ]
    return str(yaml.safe_dump(data, sort_keys=False))


# -- one batch on one carrier ---------------------------------------------------------


@dataclass
class CarrierRun:
    carrier: Carrier
    grades: dict[str, dict[str, Any]]
    versions: list[dict[str, Any]]
    cells: dict[str, dict[str, Any]]
    events: list[dict[str, Any]]
    batch_run_s: float
    total_s: float


def _grade_rows(store: Path) -> dict[str, dict[str, Any]]:
    """trial_id -> the final grade record from the result store."""
    connection = sqlite3.connect(store)
    try:
        rows = connection.execute(
            "SELECT trial_id, tier, rule_id, payload FROM grade ORDER BY trial_id, version"
        ).fetchall()
    finally:
        connection.close()
    grades: dict[str, dict[str, Any]] = {}
    for trial_id, tier, rule_id, payload in rows:
        record = json.loads(payload)
        decided = (record.get("decision") or {}).get("rule_id")
        grades[trial_id] = {"tier": tier, "rule_id": rule_id or decided, "payload": record}
    return grades


def _trial_versions(store: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(store)
    try:
        rows = connection.execute("SELECT extensions FROM trial").fetchall()
    finally:
        connection.close()
    return [json.loads(extensions).get("versions") for (extensions,) in rows]


def _replay_legal(events: list[dict[str, Any]]) -> None:
    by_trial: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        trial_id = event.get("trial_id")
        if isinstance(trial_id, str):
            by_trial.setdefault(trial_id, []).append(event)
    assert len(by_trial) == TRIALS, f"trial events for {sorted(by_trial)}"
    for trial_id, trial_events in sorted(by_trial.items()):
        machine = TrialMachine(trial_id)
        transitions = 0
        for event in trial_events:
            transition = (event.get("payload") or {}).get("transition")
            if not isinstance(transition, dict):
                continue
            transitions += 1
            try:
                machine.apply(str(transition["event"]))
            except IllegalTransition as exc:
                pytest.fail(f"{trial_id}: illegal transition {transition}: {exc}")
        assert transitions > 0, f"{trial_id}: no state transitions recorded"


def _run_batch(harness: Any, carrier: Carrier) -> CarrierRun:
    """config validate -> register -> probe -> plan -> freeze -> run -> report."""
    started = time.monotonic()

    def cli(name: str, *argv: str) -> subprocess.CompletedProcess[str]:
        result = harness.run_cli(name, *argv)
        assert result.returncode == 0, f"{carrier.adapter} {name}: {result.stderr[-2000:]}"
        return result

    cli("config-validate", "config", "validate", *harness.options())
    cli("models-register", "models", "register", *harness.options(with_batch=False))
    probed = cli("models-probe", "models", "probe", *harness.options(with_batch=False))
    assert probed.stdout.count("probes") == 3
    planned = cli("batch-plan", "batch", "plan", *harness.options())
    for task in TASK_IDS:
        for model in (REF_MODEL, EMPTY_MODEL):
            assert f"{task}.{model}.terminus" in planned.stdout
    frozen = cli("batch-freeze", "batch", "freeze", *harness.options())
    assert "version matrix hash" in frozen.stdout

    run_started = time.monotonic()
    ran = cli("batch-run", "batch", "run", *harness.options())
    batch_run_s = time.monotonic() - run_started
    events_path = harness.events_dir / f"{BATCH_ID}.jsonl"
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    invalidated = sorted(
        {
            str((event.get("payload") or {}).get("reason"))[:400]
            for event in events
            if event.get("type") == "trial.invalidated"
        }
    )
    assert not invalidated, f"{carrier.adapter}: trials were invalidated: {invalidated}"
    # batch run verifies every trial row against the event log (verify_projection) and
    # exits non-zero on a row the log does not derive.
    assert f"event projection: {TRIALS} trial row(s) checked" in ran.stdout
    (harness.reports / "batch-report.md").write_text(ran.stdout, encoding="utf-8")

    release = harness.work / "release"
    reported = cli(
        "batch-report", "batch", "report", "--json", "--release", str(release), *harness.options()
    )
    report = json.loads(reported.stdout)
    assert report["batch_id"] == BATCH_ID
    assert len(report["cells"]) == 6

    drilldown = json.loads((release / "drilldown.json").read_text(encoding="utf-8"))
    assert len(drilldown) == 6
    for entry in drilldown:
        assert entry["sandbox_adapter"] == carrier.adapter, entry
        assert entry["declared_isolation"] == carrier.declared_isolation, entry
        assert len(entry["trials"]) == EPOCHS
    manifest = json.loads((release / "versions.json").read_text(encoding="utf-8"))
    assert set(manifest["provider_environments"]) == {carrier.instance}
    frozen_env = manifest["provider_environments"][carrier.instance]
    assert frozen_env["runtime"] == dict(carrier.runtime), frozen_env
    assert frozen_env["guest"]["kernel"] == carrier.guest_kernel, frozen_env
    isolation = (release / "isolation.md").read_text(encoding="utf-8")
    assert f"## {carrier.instance} ({carrier.adapter})" in isolation

    return CarrierRun(
        carrier=carrier,
        grades=_grade_rows(harness.store_path),
        versions=_trial_versions(harness.store_path),
        cells={cell["cell_id"]: cell for cell in report["cells"]},
        events=events,
        batch_run_s=batch_run_s,
        total_s=time.monotonic() - started,
    )


def _check_run(run: CarrierRun, harness: Any, images: Mapping[str, PinnedImage]) -> None:
    """Grades, canary scans, event order, gateway usage and recorded environments."""
    carrier = run.carrier
    assert len(run.grades) == TRIALS, sorted(run.grades)
    for trial_id, grade in sorted(run.grades.items()):
        if parse_trial_id(trial_id).model == REF_MODEL:
            assert grade["tier"] == "P", f"{carrier.adapter} {trial_id}: {grade}"
        else:
            assert grade["tier"] == "F", f"{carrier.adapter} {trial_id}: {grade}"
            assert grade["rule_id"] in EMPTY_RULE_IDS, f"{carrier.adapter} {trial_id}: {grade}"
    for cell_id, cell in run.cells.items():
        assert cell["status"] == "scored", cell
        expected = "P" if f".{REF_MODEL}." in cell_id else "F"
        assert cell["tier"] == expected, f"{carrier.adapter} {cell_id}: {cell}"

    # canary: injected and scanned clean in every trial, no cross-trial hit
    scans = {
        event["trial_id"]: event["payload"]
        for event in run.events
        if event.get("type") == "canary.scanned"
    }
    assert set(scans) == set(run.grades), sorted(scans)
    for trial_id, scan in scans.items():
        assert scan["passed"] is True and scan["violations"] == [], f"{trial_id}: {scan}"

    # every sandbox ran on this carrier, from the pinned pack image
    provisioned = [event for event in run.events if event.get("type") == "sandbox.provisioned"]
    assert len(provisioned) == 2 * TRIALS
    for event in provisioned:
        payload = event["payload"]
        assert payload["provider"] == carrier.instance, payload
        task_id = parse_trial_id(event["trial_id"]).task_id
        assert payload["template_digest"] == images[task_id].docker_id, payload
        assert payload["guest"]["status"] == "measured", payload
        assert payload["guest"]["guest"]["kernel"] == carrier.guest_kernel, payload

    _replay_legal(run.events)

    # the trial rows carry the environment of the carrier that ran them
    assert len(run.versions) == TRIALS
    for versions in run.versions:
        assert versions["sandbox_adapter"] == carrier.adapter, versions
        environment = versions["execution_environment"]
        provider = environment["provider"]
        assert provider["status"] == "reported", provider
        assert provider["host"]["os"] == HOST_OS_FAMILY[platform.system()]
        assert provider["runtime"] == dict(carrier.runtime), provider
        assert provider["guest"]["kernel"] == carrier.guest_kernel, provider
        for role in ("agent", "verifier"):
            sandbox = environment["sandboxes"][role]
            assert sandbox["status"] == "measured", sandbox
            assert sandbox["provider_instance"] == carrier.instance, sandbox
            assert sandbox["guest"]["os"] == "linux"
            assert sandbox["guest"]["kernel"] == carrier.guest_kernel, sandbox

    # gateway usage equals the runner-reported tokens of every trial
    for trial_id in run.grades:
        records = harness.gateway.admin_json(f"/admin/usage?trial_id={trial_id}")
        assert isinstance(records, list) and records, f"no gateway usage for {trial_id}"
        steps = [
            event["payload"]
            for event in run.events
            if event.get("trial_id") == trial_id and event.get("type") == "agent.step"
        ]
        assert (
            sum(int(record["input_tokens"]) for record in records),
            sum(int(record["output_tokens"]) for record in records),
        ) == (
            sum(int(step.get("input_tokens") or 0) for step in steps),
            sum(int(step.get("output_tokens") or 0) for step in steps),
        ), f"{carrier.adapter} {trial_id}: gateway usage differs from the runner transcript"


def _grade_signature(grade: Mapping[str, Any]) -> dict[str, Any]:
    """What a grade decides, without timings: tier, rule, penalty and its breakdown."""
    record = grade["payload"]
    return {
        "tier": grade["tier"],
        "rule_id": grade["rule_id"],
        "penalty": record.get("penalty"),
        "penalty_breakdown": record.get("penalty_breakdown"),
        "hard_gates_failed": (record.get("hard_gate_summary") or {}).get("failed"),
        "defects": record.get("defect_summary"),
    }


def _summary(run: CarrierRun) -> dict[str, Any]:
    return {
        "adapter": run.carrier.adapter,
        "instance": run.carrier.instance,
        "batch_run_s": round(run.batch_run_s, 1),
        "total_s": round(run.total_s, 1),
        "trials": {
            trial_id: _grade_signature(grade) for trial_id, grade in sorted(run.grades.items())
        },
        "cells": {
            cell_id: {key: cell.get(key) for key in ("status", "tier")}
            for cell_id, cell in sorted(run.cells.items())
        },
        "execution_environment": run.versions[0]["execution_environment"],
    }


# -- the scenario ----------------------------------------------------------------------


@pytest.mark.e2e
def test_same_packs_grade_alike_on_docker_and_apple_container(
    tmp_path_factory: pytest.TempPathFactory,
    fake_server: object,
    gateway_factory: Any,
    harness_factory: Any,
    container_cli: str,
    pinned_images: dict[str, PinnedImage],
    parity_registry: Path,
    apple_vm_kernel: str,
) -> None:
    shutil.rmtree(AC03_REPORTS, ignore_errors=True)
    example = (REPO / "config" / "examples" / "providers.yaml").read_text(encoding="utf-8")
    plans = [
        (_docker_carrier(), example),
        (_apple_carrier(container_cli, apple_vm_kernel), _apple_providers_yaml(example)),
    ]
    runs: list[CarrierRun] = []
    for carrier, providers_yaml in plans:
        work = tmp_path_factory.mktemp(f"ac03-{carrier.adapter}")
        reports = AC03_REPORTS / carrier.adapter
        before = _container_ids(container_cli)
        with gateway_factory(work, reports / "gateway" / "gateway.log") as gateway:
            harness = harness_factory(
                work, gateway, fake_server, reports, templates_registry=parity_registry
            )
            (harness.config_dir / "providers.yaml").write_text(providers_yaml, encoding="utf-8")
            run = _run_batch(harness, carrier)
            _check_run(run, harness, pinned_images)
        runs.append(run)
        # every sandbox of the run is gone from its carrier
        sandbox_ids = {
            str(event["payload"]["sandbox_id"])
            for event in run.events
            if event.get("type") == "sandbox.provisioned"
        }
        if carrier.adapter == "docker":
            alive = {
                sandbox_id
                for sandbox_id in sandbox_ids
                if _run(["docker", "container", "inspect", sandbox_id]).returncode == 0
            }
        else:
            assert all(sandbox_id.startswith(PREFIX) for sandbox_id in sandbox_ids)
            alive = sandbox_ids & _container_ids(container_cli)
        assert not alive, f"{carrier.adapter} sandboxes survived the batch: {sorted(alive)}"
        leaked = {
            name for name in _container_ids(container_cli) - before if name.startswith(PREFIX)
        }
        assert not leaked, f"containers created during the {carrier.adapter} run remain: {leaked}"

    docker_run, apple_run = runs
    # The carriers really differ, and each environment came from its own carrier.
    docker_env = docker_run.versions[0]["execution_environment"]["provider"]
    apple_env = apple_run.versions[0]["execution_environment"]["provider"]
    assert docker_env["runtime"]["name"] != apple_env["runtime"]["name"]
    assert docker_run.carrier.guest_kernel != apple_run.carrier.guest_kernel
    # Same Mac under both carriers.
    assert docker_env["host"] == apple_env["host"], (docker_env["host"], apple_env["host"])

    # Same packs, same agents: every trial grades identically on both carriers.
    assert set(docker_run.grades) == set(apple_run.grades)
    mismatches = {
        trial_id: (
            _grade_signature(docker_run.grades[trial_id]),
            _grade_signature(apple_run.grades[trial_id]),
        )
        for trial_id in docker_run.grades
        if _grade_signature(docker_run.grades[trial_id])
        != _grade_signature(apple_run.grades[trial_id])
    }
    assert not mismatches, f"grades differ across carriers: {json.dumps(mismatches, indent=2)}"
    for cell_id, cell in docker_run.cells.items():
        other = apple_run.cells[cell_id]
        assert (cell["status"], cell["tier"]) == (other["status"], other["tier"]), cell_id

    summary = {
        "images": {
            task_id: {
                "source_tag": image.source_tag,
                "tag": image.tag,
                "docker_id": image.docker_id,
                "apple_digest": image.apple_digest,
            }
            for task_id, image in pinned_images.items()
        },
        "apple_vm_kernel": apple_vm_kernel,
        "runs": [_summary(run) for run in runs],
    }
    (AC03_REPORTS / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    for run in runs:
        for trial_id, grade in sorted(run.grades.items()):
            signature = _grade_signature(grade)
            print(
                f"{run.carrier.adapter:16} {trial_id:44} tier={signature['tier']} "
                f"rule={signature['rule_id']} penalty={signature['penalty']}"
            )
