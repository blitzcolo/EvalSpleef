"""spleef batch plan/freeze/run/report against the mock sandbox and scripted runner.

The scripted runner entry point is the empty agent: every hard gate fails, so the
cells land on a failing tier; the tests assert the wiring (plan, guards, events,
report shape), not model quality.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from evalspleef.control.cli.app import app


def _batch_args(env: Path, args: list[str]) -> list[str]:
    return [*args, "--batch", str(env / "batch.yaml")]


def test_plan_after_probe(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    result = cli_runner.invoke(app, ["batch", "plan", *_batch_args(env, args)])
    assert result.exit_code == 0, result.output
    assert "logic-001.demo.terminus" not in result.output  # shell is scripted
    assert "logic-001.demo.scripted" in result.output
    as_json = cli_runner.invoke(app, ["batch", "plan", "--json", *_batch_args(env, args)])
    plan = json.loads(as_json.output)
    assert plan["budget"]["within_cap"] is True


def test_plan_requires_registered_models(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    result = cli_runner.invoke(app, ["batch", "plan", *_batch_args(env, args)])
    assert result.exit_code == 1
    assert "not registered" in result.output


def test_freeze_stores_record(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    result = cli_runner.invoke(app, ["batch", "freeze", *_batch_args(env, args)])
    assert result.exit_code == 0, result.output
    assert "version matrix hash" in result.output
    assert (Path(env).parent / "state" / "freezes" / "2026-11-b1.json").is_file()


def test_failed_freeze_neither_warms_nor_retires(
    cli_runner: CliRunner, args: list[str], env: Path
) -> None:
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    batch = env / "batch.yaml"
    batch.write_text(
        batch.read_text(encoding="utf-8").replace("max_total_usd: 100", "max_total_usd: 0.0001"),
        encoding="utf-8",
    )

    run = cli_runner.invoke(app, ["batch", "run", *_batch_args(env, args)])

    assert run.exit_code != 0
    assert "warm " not in run.output and "retire " not in run.output
    listed = cli_runner.invoke(app, ["models", "list", "--json", *args])
    assert listed.exit_code == 0, listed.output
    assert [(row["state"], row["retired_at"]) for row in json.loads(listed.output)] == [
        ("ready", None)
    ]


def test_batch_run_and_report(
    cli_runner: CliRunner, args: list[str], env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A provider that can name the deployment behind an endpoint publishes it under the
    # SPI's neutral key; trial.versions records it from there.
    from evalspleef_model_openai_compat.provider import OpenAICompatibleProvider

    from evalspleef.spi.model import DEPLOYMENT_ID_KEY, DeploymentPlan, EndpointHandle, ModelSpec

    deploy = OpenAICompatibleProvider.deploy

    async def named_deploy(
        self: OpenAICompatibleProvider, spec: ModelSpec, plan: DeploymentPlan
    ) -> EndpointHandle:
        handle = await deploy(self, spec, plan)
        handle.external_ids[DEPLOYMENT_ID_KEY] = "dep-7"
        return handle

    monkeypatch.setattr(OpenAICompatibleProvider, "deploy", named_deploy)
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0

    # The scripted runner replays a round that writes the declared answer file;
    # without it the empty agent still exercises the full feedback loop.
    from evalspleef.testing.scripted_runner import ScriptedRound, ScriptedRunner

    rounds = [
        ScriptedRound(files={"answer.json": "{}"}, final_message="done"),
    ]
    monkeypatch.setattr(
        "evalspleef.control.cli.context.AppContext.runner_classes",
        lambda self: {"scripted": lambda: ScriptedRunner(rounds)},  # type: ignore[dict-item]
    )

    run = cli_runner.invoke(app, ["batch", "run", *_batch_args(env, args)])
    assert run.exit_code == 0, run.output
    assert "批次报告" in run.output

    report = cli_runner.invoke(app, ["batch", "report", "--json", *_batch_args(env, args)])
    assert report.exit_code == 0, report.output
    data = json.loads(report.output)
    assert data["batch_id"] == "2026-11-b1"
    assert len(data["cells"]) == 1
    cell = data["cells"][0]
    assert cell["trial_count"] == 1
    # Only the manual-review queue is left, which is a result: the run above exited 0.
    assert cell["status"] == "manual"
    # trial rows landed in the result store and replay as legal event history
    events_dir = Path(env).parent / "events"
    events_file = events_dir / "2026-11-b1.jsonl"
    assert events_file.is_file()
    lines = [json.loads(line) for line in events_file.read_text(encoding="utf-8").splitlines()]
    assert any(line["type"] == "trial.created" for line in lines)

    # trial.versions is recorded when the agent starts and copied onto the trial row.
    started = [line for line in lines if line["type"] == "trial.started"]
    assert len(started) == 1
    versions = started[0]["payload"]["versions"]
    assert versions["model_version"] == "demo@1"
    assert versions["model_source"] == "vendor"
    assert versions["deployment_adapter"] == "openai-compatible"
    assert versions["deployment_id"] == "dep-7"
    assert versions["freeze_hash"].startswith("sha256:")
    assert versions["gateway_version"]
    assert versions["probe_report_id"]
    assert versions["endpoint_base_url_hash"].startswith("sha256:")
    assert versions["shell_version"].startswith("scripted@")
    assert versions["sandbox_adapter"] and versions["provider_instance"]
    with sqlite3.connect(Path(env).parent / "store.sqlite3") as db:
        (row_extensions,) = db.execute("SELECT extensions FROM trial").fetchone()
    row_versions = json.loads(row_extensions)["versions"]
    # The verifier sandbox is leased after trial.started; its measured guest joins the row.
    row_versions["execution_environment"]["sandboxes"].pop("verifier", None)
    assert row_versions == versions

    # The cost ledger has the trial's spend; a bill that disagrees fails the reconciliation.
    costs = cli_runner.invoke(app, ["batch", "costs", "--batch-id", "2026-11-b1", "--json"])
    kinds = {row["kind"]: row for row in json.loads(costs.output)["kinds"]}
    assert set(kinds) == {"tokens", "gpu", "sandbox", "infra"}
    bill = Path(env).parent / "bill.json"
    bill.write_text(
        json.dumps({"items": [{"id": "tx-1", "product_category": "llm", "amount": 10.0}]}),
        encoding="utf-8",
    )
    # The bill is parsed by the account's model provider, so the config gains one
    # whose adapter implements parse_bill.
    providers = env / "providers.yaml"
    providers.write_text(
        providers.read_text(encoding="utf-8").replace(
            "sandbox_instances:",
            "  novita:\n    adapter: novita-dedicated\n\nsandbox_instances:",
        ),
        encoding="utf-8",
    )
    mismatch = cli_runner.invoke(
        app,
        [
            "batch",
            "costs",
            "--batch-id",
            "2026-11-b1",
            "--bill",
            str(bill),
            "--account",
            "novita",
            "--providers",
            str(providers),
        ],
    )
    assert mismatch.exit_code == 1
    assert "tokens" in mismatch.output and "MISMATCH" in mismatch.output

    # The trial paused for a human; the operator commands decide and then void it.
    trial_id = "logic-001.demo.scripted.e1.t01"
    listed = cli_runner.invoke(app, ["trials", "list", "--batch-id", "2026-11-b1"])
    assert f"{trial_id}\tpaused_manual\tmanual\t-" in listed.output
    decision = ["--tier", "B", "--penalty", "4", "--reviewer", "alice"]
    resolved = cli_runner.invoke(
        app,
        ["trials", "resolve", "--trial-id", trial_id, *decision, "--reason", "judge outage, read"],
    )
    assert resolved.exit_code == 0, resolved.output
    listed = cli_runner.invoke(app, ["trials", "list", "--batch-id", "2026-11-b1"])
    assert f"{trial_id}\tscored\tvalid\tv1 B (human)" in listed.output
    voided = cli_runner.invoke(
        app, ["trials", "void", "--trial-id", trial_id, "--reason", "task bug found"]
    )
    assert voided.exit_code == 0, voided.output
    listed = cli_runner.invoke(app, ["trials", "list", "--batch-id", "2026-11-b1"])
    assert f"{trial_id}\tinvalid\tinvalid" in listed.output
    refused = cli_runner.invoke(
        app, ["trials", "regrade", "--trial-id", trial_id, "--reason", "after void"]
    )
    assert refused.exit_code == 1


@pytest.mark.parametrize("command", ["freeze", "run"])
def test_a_model_without_prices_cannot_be_frozen_or_run(
    cli_runner: CliRunner, args: list[str], env: Path, command: str
) -> None:
    models = env / "models.yaml"
    models.write_text(
        models.read_text(encoding="utf-8").replace(
            ", pricing: {input_per_mtok: 0, output_per_mtok: 0}", "", 1
        ),
        encoding="utf-8",
    )
    assert cli_runner.invoke(app, ["models", "register", "--model", "demo", *args]).exit_code == 0
    assert cli_runner.invoke(app, ["models", "probe", "--model", "demo", *args]).exit_code == 0
    result = cli_runner.invoke(app, ["batch", command, *_batch_args(env, args)])
    assert result.exit_code == 1, result.output
    assert "no deployment.params.pricing for demo" in result.output
    assert not (Path(env).parent / "state" / "freezes" / "2026-11-b1.json").exists()


JUDGE_YAML = """\
  - slug: judgy
    family: judgefam
    roles: [judge]
    source: {type: vendor, vendor: evalspleef, model_id: judgy}
    deployment: {adapter: openai-compatible, account: fake, params: {model: demo, pricing: {input_per_mtok: 0, output_per_mtok: 0}}}
    declared_context_tokens: 32768
    declared_max_output_tokens: 4096
    pricing_ref: "assumed:judgy@2026-11"
    license: MIT
    owner: test
"""


def _register_demo_and_judge(cli_runner: CliRunner, args: list[str], env: Path) -> None:
    """A batch with one judge; both models registered and probed green."""
    models = env / "models.yaml"
    models.write_text(models.read_text(encoding="utf-8") + JUDGE_YAML, encoding="utf-8")
    batch = env / "batch.yaml"
    batch.write_text(
        batch.read_text(encoding="utf-8").replace("judges: []", "judges: [judgy]"),
        encoding="utf-8",
    )
    for model in ("demo", "judgy"):
        registered = cli_runner.invoke(app, ["models", "register", "--model", model, *args])
        assert registered.exit_code == 0, registered.output
        probed = cli_runner.invoke(app, ["models", "probe", "--model", model, *args])
        assert probed.exit_code == 0, probed.output


def _fail_judges_p14(env: Path) -> None:
    """Reproduce the incident state: P-14 failed, so the endpoint is probe_failed."""
    with sqlite3.connect(Path(env).parent / "store.sqlite3") as db:
        (endpoint_id, observed) = db.execute(
            "SELECT endpoint_id, observed FROM endpoint WHERE model_slug = 'judgy'"
        ).fetchone()
        report_id = json.loads(observed)["report_id"]
        db.execute(
            "UPDATE endpoint SET state = 'probe_failed' WHERE endpoint_id = ?", (endpoint_id,)
        )
        (results,) = db.execute(
            "SELECT results FROM probe_report WHERE report_id = ?", (report_id,)
        ).fetchone()
        checks = json.loads(results)["checks"]
        broken = [
            {**check, "passed": False} if check["probe_id"] == "P-14" else check for check in checks
        ]
        db.execute(
            "UPDATE probe_report SET results = ? WHERE report_id = ?",
            (json.dumps({"checks": broken}), report_id),
        )


def test_freeze_refuses_judge_whose_required_probe_failed(
    cli_runner: CliRunner, args: list[str], env: Path
) -> None:
    _register_demo_and_judge(cli_runner, args, env)
    _fail_judges_p14(env)

    result = cli_runner.invoke(app, ["batch", "freeze", *_batch_args(env, args)])

    assert result.exit_code == 1
    assert "judge judgy" in result.output
    assert "probe_failed" in result.output
    assert "P-14" in result.output
    assert not (Path(env).parent / "state" / "freezes" / "2026-11-b1.json").exists()


def test_batch_run_refuses_to_start_when_a_judge_is_not_admitted(
    cli_runner: CliRunner, args: list[str], env: Path
) -> None:
    _register_demo_and_judge(cli_runner, args, env)
    _fail_judges_p14(env)

    run = cli_runner.invoke(app, ["batch", "run", *_batch_args(env, args)])

    assert run.exit_code != 0
    assert "judge judgy" in run.output
    assert "P-14" in run.output
    assert "warm ep_" not in run.output
    assert "retire ep_" not in run.output


def test_freeze_record_covers_the_judges_probe_report(
    cli_runner: CliRunner, args: list[str], env: Path
) -> None:
    _register_demo_and_judge(cli_runner, args, env)

    result = cli_runner.invoke(app, ["batch", "freeze", *_batch_args(env, args)])

    assert result.exit_code == 0, result.output
    record = json.loads(
        (Path(env).parent / "state" / "freezes" / "2026-11-b1.json").read_text(encoding="utf-8")
    )
    judge_endpoint = record["judges"]["judgy"]["endpoint_id"]
    assert judge_endpoint.startswith("ep_judgy_")
    assert record["probe_report_ids"][judge_endpoint].startswith("pr_")
