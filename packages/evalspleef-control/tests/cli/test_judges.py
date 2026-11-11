"""spleef judges/batch agreement commands against a store built by a real batch run.

Every test runs the production writers end to end (mock sandbox, scripted runner,
two gateway judges served by the fake OpenAI server) and then exercises the
read-side commands: the judge.verdict events carry the per-judge votes the
commands analyse.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from evalspleef.control.cli.app import app
from evalspleef.testing.fake_openai import FakeOpenAIServer

REPO = Path(__file__).resolve().parents[4]

#: logic-001's llm_judge items (rubric.yaml); script items are not judge-decided.
JUDGED_ITEMS = ("R6", "R7", "R8", "R9", "R10")

#: judge-a agrees with judge-b except one shared fail: perfect agreement, two categories.
ALIGNED = {
    "judge-a": dict(zip(JUDGED_ITEMS, ["pass"] * 4 + ["fail"], strict=True)),
    "judge-b": dict(zip(JUDGED_ITEMS, ["pass"] * 4 + ["fail"], strict=True)),
}
#: judge-b fails R8/R9 as well: 3/5 agreement, kappa 2/7, alpha 1/4, two panel splits.
SPLIT = {
    "judge-a": dict(zip(JUDGED_ITEMS, ["pass"] * 4 + ["fail"], strict=True)),
    "judge-b": dict(zip(JUDGED_ITEMS, ["pass", "pass", "fail", "fail", "fail"], strict=True)),
}
#: both judges pass every item: percent 1.0, but alpha (De=0) and kappa (pe=1) are undefined.
UNANIMOUS = {
    "judge-a": dict.fromkeys(JUDGED_ITEMS, "pass"),
    "judge-b": dict.fromkeys(JUDGED_ITEMS, "pass"),
}

JUDGE_MODELS_YAML = """\
  - slug: judge-a
    family: judgefam-a
    roles: [judge]
    source: {type: vendor, vendor: evalspleef, model_id: judge-a}
    deployment: {adapter: openai-compatible, account: fake, params: {model: demo, pricing: {input_per_mtok: 0, output_per_mtok: 0}}}
    declared_context_tokens: 32768
    declared_max_output_tokens: 4096
    pricing_ref: "assumed:judge-a@2026-11"
    license: MIT
    owner: test
  - slug: judge-b
    family: judgefam-b
    roles: [judge]
    source: {type: vendor, vendor: evalspleef, model_id: judge-b}
    deployment: {adapter: openai-compatible, account: fake, params: {model: demo, pricing: {input_per_mtok: 0, output_per_mtok: 0}}}
    declared_context_tokens: 32768
    declared_max_output_tokens: 4096
    pricing_ref: "assumed:judge-b@2026-11"
    license: MIT
    owner: test
"""

BATCH_WITH_JUDGES = """\
batch: {batch_id}
models: [demo]
shells: {unified: scripted}
tasks: {include: [logic-001]}
epochs: 1
budget: {max_total_usd: 100}
endpoint_policy: {warm_before_run: true, retire_after: terminate}
judges: [judge-a, judge-b]
pricing_table: "2026-11"
"""


def _write_configs(env: Path, batch_id: str) -> None:
    models = env / "models.yaml"
    if "judge-a" not in models.read_text(encoding="utf-8"):
        models.write_text(models.read_text(encoding="utf-8") + JUDGE_MODELS_YAML, encoding="utf-8")
    (env / "batch.yaml").write_text(
        BATCH_WITH_JUDGES.replace("{batch_id}", batch_id), encoding="utf-8"
    )


def _patch_judge_votes(
    monkeypatch: pytest.MonkeyPatch, server: FakeOpenAIServer, pattern: dict[str, dict[str, str]]
) -> None:
    """Answer judge calls per (judge endpoint, rubric item); everything else stays stock."""
    real_generate = server._generate
    item_re = re.compile(r"rubric_item_id: (R\d+)")

    def generate(body: dict[str, object]) -> tuple[dict[str, object], str, int]:
        model = str(body.get("model", ""))
        for slug, votes in pattern.items():
            if model.startswith(f"ep_{slug}"):
                text = "".join(
                    str(message.get("content", ""))
                    for message in body.get("messages", [])  # type: ignore[union-attr]
                )
                match = item_re.search(text)
                # Only judge votes are scripted; probes and other calls stay stock.
                if match is None:
                    return real_generate(body)  # type: ignore[arg-type]
                verdict = votes[match.group(1)]
                return (
                    {"role": "assistant", "content": json.dumps({"verdict": verdict})},
                    "stop",
                    3,
                )
        return real_generate(body)  # type: ignore[arg-type]

    monkeypatch.setattr(server, "_generate", generate)


def _run_batch(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    server: FakeOpenAIServer,
    pattern: dict[str, dict[str, str]],
    batch_id: str = "2026-11-b1",
) -> None:
    _write_configs(env, batch_id)
    register = cli_runner.invoke(
        app,
        [
            "models",
            "register",
            "--model",
            "demo",
            "--model",
            "judge-a",
            "--model",
            "judge-b",
            *args,
        ],
    )
    assert register.exit_code == 0, register.output
    # Probes run before the vote patch so the judges answer the stock probe suite;
    # freeze refuses a batch whose judges are not admitted, so judges must be probed.
    for model in ("demo", "judge-a", "judge-b"):
        probed = cli_runner.invoke(app, ["models", "probe", "--model", model, *args])
        assert probed.exit_code == 0, probed.output
    _patch_judge_votes(monkeypatch, server, pattern)
    result = cli_runner.invoke(app, ["batch", "run", *args, "--batch", str(env / "batch.yaml")])
    assert result.exit_code == 0, result.output


@pytest.fixture
def aligned_batch(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> Iterator[Path]:
    """A finished batch whose two judges agree on every item (R10 fails for both)."""
    _run_batch(cli_runner, args, env, monkeypatch, fake_server, ALIGNED)
    yield env


@pytest.fixture
def unanimous_batch(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> Iterator[Path]:
    """A finished batch whose two judges pass every item (one rating category only)."""
    _run_batch(cli_runner, args, env, monkeypatch, fake_server, UNANIMOUS)
    yield env


def test_agreement_pass(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> None:
    _run_batch(cli_runner, args, env, monkeypatch, fake_server, ALIGNED)
    result = cli_runner.invoke(app, ["judges", "agreement", "--batch-id", "2026-11-b1", "--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["n"] == 5
    assert summary["judges"] == ["judge-a", "judge-b"]
    (pair,) = summary["pairs"]
    assert (pair["judge_a"], pair["judge_b"], pair["n"]) == ("judge-a", "judge-b", 5)
    assert pair["cohen_kappa"]["value"] == pytest.approx(1.0)
    assert pair["percent_agreement"]["value"] == pytest.approx(1.0)
    assert summary["alpha"]["value"] == pytest.approx(1.0)
    assert summary["alpha"]["verdict"] == "usable"
    assert summary["panel_splits"] == 0
    assert summary["percent_agreement"]["value"] == pytest.approx(1.0)
    assert summary["gate"]["status"] == "pass"
    assert summary["gate"]["undefined"] == []


def test_agreement_below_gate(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> None:
    _run_batch(cli_runner, args, env, monkeypatch, fake_server, SPLIT)
    result = cli_runner.invoke(app, ["judges", "agreement", "--batch-id", "2026-11-b1", "--json"])
    assert result.exit_code == 1, result.output
    summary = json.loads(result.output)
    (pair,) = summary["pairs"]
    # a=[P,P,P,P,F] vs b=[P,P,F,F,F]: po=0.6, pe=(4/5)(2/5)+(1/5)(3/5)=0.44, kappa=2/7;
    # alpha: n_P=6, n_F=4, Do=0.4, De=48/90 -> 1/4; splits=2
    assert pair["cohen_kappa"]["value"] == pytest.approx(2 / 7)
    assert pair["percent_agreement"]["value"] == pytest.approx(0.6)
    assert summary["alpha"]["value"] == pytest.approx(0.25)
    assert summary["panel_splits"] == 2
    assert summary["gate"]["status"] == "below"


def test_agreement_undefined_alpha_is_undecided(
    cli_runner: CliRunner, unanimous_batch: Path
) -> None:
    """All-identical votes leave alpha undefined (De=0); the gate must not PASS."""
    result = cli_runner.invoke(app, ["judges", "agreement", "--batch-id", "2026-11-b1", "--json"])
    assert result.exit_code == 3, result.output
    summary = json.loads(result.output)
    assert summary["percent_agreement"]["value"] == pytest.approx(1.0)
    assert summary["alpha"]["value"] is None
    assert summary["gate"]["status"] == "undecided"
    assert [entry["metric"] for entry in summary["gate"]["undefined"]] == ["alpha"]
    assert "De=0" in summary["gate"]["undefined"][0]["reason"]

    human = cli_runner.invoke(app, ["judges", "agreement", "--batch-id", "2026-11-b1"])
    assert human.exit_code == 3, human.output
    assert "alpha undefined" in human.output
    assert "gate not decided" in human.output
    assert "UNDECIDED" in human.output
    assert "PASS" not in human.output
    assert "pooled percent agreement: 1.000" in human.output


def test_agreement_not_enough_data(cli_runner: CliRunner, env: Path, args: list[str]) -> None:
    result = cli_runner.invoke(app, ["judges", "agreement", "--batch-id", "2099-01-b9"])
    assert result.exit_code == 3, result.output
    assert "error:" in result.output


def test_export_labels_is_blind(cli_runner: CliRunner, aligned_batch: Path) -> None:
    out = aligned_batch.parent / "sheet.jsonl"
    result = cli_runner.invoke(
        app, ["judges", "export-labels", "--batch-id", "2026-11-b1", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 5
    for row in rows:
        assert set(row) == {"trial_id", "task_id", "item_id", "rubric_text", "artifacts", "label"}
        assert row["trial_id"] == "logic-001.demo.scripted.e1.t01"
        assert row["task_id"] == "logic-001"
        assert row["label"] is None
        assert row["rubric_text"]
    assert {row["item_id"] for row in rows} == set(JUDGED_ITEMS)
    assert all(isinstance(row["artifacts"], list) for row in rows)


def test_calibrate_pass_and_below_gate(cli_runner: CliRunner, aligned_batch: Path) -> None:
    # labels equal to the aligned panel: both judges sit at kappa 1.0 / percent 1.0
    out = aligned_batch.parent / "sheet.jsonl"
    cli_runner.invoke(
        app, ["judges", "export-labels", "--batch-id", "2026-11-b1", "--out", str(out)]
    )
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    for row in rows:
        row["label"] = "fail" if row["item_id"] == "R10" else "pass"
    filled = aligned_batch.parent / "filled.jsonl"
    filled.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = cli_runner.invoke(
        app, ["judges", "calibrate", "--batch-id", "2026-11-b1", "--labels", str(filled), "--json"]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["n"] == 5
    assert [row["passed"] for row in summary["judges"]] == [True, True]
    assert all(row["cohen_kappa"]["value"] == pytest.approx(1.0) for row in summary["judges"])
    assert summary["alpha"]["value"] == pytest.approx(1.0)
    assert summary["gate"]["status"] == "pass"

    # one label flipped: every judge drops below the dual gate (percent 0.8, kappa < 0.70)
    for row in rows:
        if row["item_id"] == "R8":
            row["label"] = "fail"
    flipped = aligned_batch.parent / "flipped.jsonl"
    flipped.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = cli_runner.invoke(
        app, ["judges", "calibrate", "--batch-id", "2026-11-b1", "--labels", str(flipped), "--json"]
    )
    assert result.exit_code == 1, result.output
    summary = json.loads(result.output)
    assert all(not row["passed"] for row in summary["judges"])
    assert summary["gate"]["status"] == "below"


def test_calibrate_undefined_kappa_is_undecided(
    cli_runner: CliRunner, unanimous_batch: Path
) -> None:
    """Labels identical to a one-category panel leave kappa undefined (pe=1)."""
    rows = [
        {"trial_id": "logic-001.demo.scripted.e1.t01", "item_id": item, "label": "pass"}
        for item in JUDGED_ITEMS
    ]
    filled = unanimous_batch.parent / "labels-unanimous.jsonl"
    filled.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = cli_runner.invoke(
        app, ["judges", "calibrate", "--batch-id", "2026-11-b1", "--labels", str(filled), "--json"]
    )
    assert result.exit_code == 3, result.output
    summary = json.loads(result.output)
    assert all(row["percent_agreement"]["value"] == pytest.approx(1.0) for row in summary["judges"])
    assert all(row["cohen_kappa"]["value"] is None for row in summary["judges"])
    assert all(row["passed"] is None for row in summary["judges"])
    assert summary["gate"]["status"] == "undecided"
    assert {entry["judge"] for entry in summary["gate"]["undefined"]} == {"judge-a", "judge-b"}
    assert all(entry["metric"] == "cohen_kappa" for entry in summary["gate"]["undefined"])

    human = cli_runner.invoke(
        app, ["judges", "calibrate", "--batch-id", "2026-11-b1", "--labels", str(filled)]
    )
    assert human.exit_code == 3, human.output
    assert "cohen_kappa for judge-a undefined" in human.output
    assert "gate not decided" in human.output
    assert "UNDECIDED" in human.output
    assert "PASS" not in human.output


def test_calibrate_split_panel_only_one_judge_fails(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> None:
    _run_batch(cli_runner, args, env, monkeypatch, fake_server, SPLIT)
    # labels equal judge-a's votes: judge-a passes, judge-b (b=[P,P,F,F,F]) is below
    rows = [
        {"trial_id": "logic-001.demo.scripted.e1.t01", "item_id": item, "label": label}
        for item, label in SPLIT["judge-a"].items()
    ]
    filled = env.parent / "labels-a.jsonl"
    filled.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    result = cli_runner.invoke(
        app, ["judges", "calibrate", "--batch-id", "2026-11-b1", "--labels", str(filled), "--json"]
    )
    assert result.exit_code == 1, result.output
    summary = json.loads(result.output)
    by_judge = {row["judge"]: row for row in summary["judges"]}
    assert by_judge["judge-a"]["passed"] is True
    assert by_judge["judge-a"]["cohen_kappa"]["value"] == pytest.approx(1.0)
    assert by_judge["judge-b"]["passed"] is False
    assert by_judge["judge-b"]["percent_agreement"]["value"] == pytest.approx(0.6)


def test_calibrate_no_matching_labels(cli_runner: CliRunner, aligned_batch: Path) -> None:
    filled = aligned_batch.parent / "empty.jsonl"
    filled.write_text(
        json.dumps({"trial_id": "logic-001.demo.scripted.e9.t99", "item_id": "R6", "label": "pass"})
        + "\n",
        encoding="utf-8",
    )
    result = cli_runner.invoke(
        app, ["judges", "calibrate", "--batch-id", "2026-11-b1", "--labels", str(filled)]
    )
    assert result.exit_code == 3, result.output


def test_seed_gold(cli_runner: CliRunner, tmp_path: Path) -> None:
    out = tmp_path / "gold.jsonl"
    result = cli_runner.invoke(
        app,
        [
            "judges",
            "seed-gold",
            "--tasks-dir",
            str(REPO / "tasks"),
            "--include",
            "logic-*",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "weaker than human labels" in result.output
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    # 3 samples x judge-decidable items: logic-001 has 5, logic-002 has 4
    assert len(rows) == 27
    assert {row["task_id"] for row in rows} == {"logic-001", "logic-002"}
    assert all(row["seed_set"] is True for row in rows)
    for row in rows:
        expected = "fail" if row["sample_id"].endswith("empty-agent") else "pass"
        assert row["label"] == expected
        assert row["rubric_text"]


def test_regrade_agreement_pass_then_disagreement(
    cli_runner: CliRunner, aligned_batch: Path, args: list[str]
) -> None:
    result = cli_runner.invoke(
        app, ["batch", "regrade-agreement", "--batch-id", "2026-11-b1", "--json"]
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["value"] == pytest.approx(1.0)
    assert summary["n"] == 1
    assert summary["disagreements"] == []

    # a human override is a stored grade the regrade does not reproduce
    resolve = cli_runner.invoke(
        app,
        [
            "trials",
            "resolve",
            "--batch-id",
            "2026-11-b1",
            "--trial-id",
            "logic-001.demo.scripted.e1.t01",
            "--tier",
            "P",
            "--penalty",
            "0",
            "--reviewer",
            "probe",
            "--reason",
            "force a disagreement",
        ],
    )
    assert resolve.exit_code == 0, resolve.output
    result = cli_runner.invoke(
        app, ["batch", "regrade-agreement", "--batch-id", "2026-11-b1", "--json"]
    )
    assert result.exit_code == 1, result.output
    summary = json.loads(result.output)
    assert summary["value"] == pytest.approx(0.0)
    (row,) = summary["disagreements"]
    assert row["stored_tier"] == "P"
    assert row["recomputed_tier"] == "F"


def test_stability_pass_then_below_gate(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
    tmp_path: Path,
) -> None:
    _run_batch(cli_runner, args, env, monkeypatch, fake_server, ALIGNED, batch_id="2026-11-b1")
    # The same config runs again into the SAME store (NFR-01 rerun stability). A hosted
    # endpoint is retired after its batch, so restore the row like an operator re-enabling
    # the same deployment before the rerun.
    with sqlite3.connect(tmp_path / "store.sqlite3") as db:
        db.execute("UPDATE endpoint SET state = 'ready' WHERE model_slug = 'demo'")
    _run_batch(cli_runner, args, env, monkeypatch, fake_server, ALIGNED, batch_id="2026-11-b2")
    result = cli_runner.invoke(
        app,
        ["batch", "stability", "--batch-id", "2026-11-b1", "--batch-id", "2026-11-b2", "--json"],
    )
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["n"] == 1
    assert summary["within_one_share"] == pytest.approx(1.0)
    assert summary["exact_agreement"] == pytest.approx(1.0)

    # a human override on b2's trial moves its cell tier far away: below the NFR-01 gate
    resolve = cli_runner.invoke(
        app,
        [
            "trials",
            "resolve",
            "--batch-id",
            "2026-11-b2",
            "--trial-id",
            "logic-001.demo.scripted.e1.t01",
            "--tier",
            "P",
            "--penalty",
            "0",
            "--reviewer",
            "probe",
            "--reason",
            "force a far tier",
        ],
    )
    assert resolve.exit_code == 0, resolve.output
    result = cli_runner.invoke(
        app,
        ["batch", "stability", "--batch-id", "2026-11-b1", "--batch-id", "2026-11-b2", "--json"],
    )
    assert result.exit_code == 1, result.output
    summary = json.loads(result.output)
    assert summary["within_one_share"] == pytest.approx(0.0)
    (row,) = summary["cells"]
    assert row["spread"] == 7  # P=7 vs F=0


def test_stability_needs_two_batches(cli_runner: CliRunner, aligned_batch: Path) -> None:
    result = cli_runner.invoke(app, ["batch", "stability", "--batch-id", "2026-11-b1"])
    assert result.exit_code == 3, result.output


#: Seed calibration (``judges calibrate --seed``): the panel judges each seed
#: sample's answer file live and the votes meet the seed labels.
SEED_STYLES = ("aligned", "always-pass", "always-fail")


def _write_seed_batch_config(env: Path) -> None:
    """Register the judge models and point batch.yaml at them (no batch run needed)."""
    models = env / "models.yaml"
    if "judge-a" not in models.read_text(encoding="utf-8"):
        models.write_text(models.read_text(encoding="utf-8") + JUDGE_MODELS_YAML, encoding="utf-8")
    (env / "batch.yaml").write_text(
        BATCH_WITH_JUDGES.replace("{batch_id}", "2026-11-b7"), encoding="utf-8"
    )


def _register_judges(cli_runner: CliRunner, args: list[str]) -> None:
    register = cli_runner.invoke(
        app,
        [
            "models",
            "register",
            "--model",
            "demo",
            "--model",
            "judge-a",
            "--model",
            "judge-b",
            *args,
        ],
    )
    assert register.exit_code == 0, register.output


def _make_seed_gold(cli_runner: CliRunner, env: Path) -> Path:
    """The logic-001 seed set: 5 llm_judge items x 3 samples (A/B pass, empty fails)."""
    gold = env.parent / "gold.jsonl"
    result = cli_runner.invoke(
        app,
        [
            "judges",
            "seed-gold",
            "--tasks-dir",
            str(REPO / "tasks"),
            "--include",
            "logic-001",
            "--out",
            str(gold),
        ],
    )
    assert result.exit_code == 0, result.output
    return gold


def _patch_seed_judges(
    monkeypatch: pytest.MonkeyPatch, server: FakeOpenAIServer, style: str
) -> None:
    """Script every judge vote; ``aligned`` agrees with the seed labels per sample."""
    assert style in SEED_STYLES
    real_generate = server._generate
    item_re = re.compile(r"rubric_item_id: (R\d+)")

    def generate(body: dict[str, object]) -> tuple[dict[str, object], str, int]:
        model = str(body.get("model", ""))
        if not (model.startswith("ep_judge-a") or model.startswith("ep_judge-b")):
            return real_generate(body)  # type: ignore[arg-type]
        text = "".join(
            str(message.get("content", ""))
            for message in body.get("messages", [])  # type: ignore[union-attr]
        )
        if item_re.search(text) is None:
            return real_generate(body)  # type: ignore[arg-type]
        # The prompt's evidence digest names the sample's answer file.
        if style == "aligned":
            verdict = "fail" if "empty-agent" in text else "pass"
        else:
            verdict = style.removeprefix("always-")
        return (
            {"role": "assistant", "content": json.dumps({"verdict": verdict})},
            "stop",
            3,
        )

    monkeypatch.setattr(server, "_generate", generate)


def _calibrate_args(args: list[str]) -> list[str]:
    """calibrate takes no --templates-registry; the fixture args carry it for
    the batch-driving commands, so drop the flag and its value here."""
    calibrate_args: list[str] = []
    skip = False
    for item in args:
        if skip:
            skip = False
            continue
        if item == "--templates-registry":
            skip = True
            continue
        calibrate_args.append(item)
    return calibrate_args


def _calibrate_seed(
    cli_runner: CliRunner, args: list[str], env: Path, gold: Path, extra: list[str] | None = None
) -> Result:
    return cli_runner.invoke(
        app,
        [
            "judges",
            "calibrate",
            "--seed",
            str(gold),
            "--batch",
            str(env / "batch.yaml"),
            *_calibrate_args(args),
            *(extra or []),
        ],
    )


def test_calibrate_seed_pass(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> None:
    _write_seed_batch_config(env)
    _register_judges(cli_runner, args)
    _patch_seed_judges(monkeypatch, fake_server, "aligned")
    gold = _make_seed_gold(cli_runner, env)
    result = _calibrate_seed(cli_runner, args, env, gold)
    assert result.exit_code == 0, result.output
    assert "seed calibration: 15 rows judged by the panel" in result.output
    assert "weaker than human labels" in result.output
    assert "judge spend $" in result.output

    result = _calibrate_seed(cli_runner, args, env, gold, extra=["--json"])
    assert result.exit_code == 0, result.output
    summary = json.loads(result.output)
    assert summary["n"] == 15
    assert [row["passed"] for row in summary["judges"]] == [True, True]
    assert all(row["cohen_kappa"]["value"] == pytest.approx(1.0) for row in summary["judges"])
    assert summary["gate"]["status"] == "pass"
    assert summary["seed"]["rows"] == 15
    assert summary["seed"]["judged"] == 15
    assert summary["seed"]["skipped"] == 0


def test_calibrate_seed_always_pass_is_below_gate(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> None:
    """A judge that passes everything disagrees with the 5 empty-agent labels."""
    _write_seed_batch_config(env)
    _register_judges(cli_runner, args)
    _patch_seed_judges(monkeypatch, fake_server, "always-pass")
    gold = _make_seed_gold(cli_runner, env)
    result = _calibrate_seed(cli_runner, args, env, gold, extra=["--json"])
    assert result.exit_code == 1, result.output
    summary = json.loads(result.output)
    # votes all pass vs labels [pass x10, fail x5]: po = 2/3 < 0.70, kappa 0.
    for row in summary["judges"]:
        assert row["percent_agreement"]["value"] == pytest.approx(2 / 3)
        assert row["cohen_kappa"]["value"] == pytest.approx(0.0)
        assert row["passed"] is False
    assert summary["gate"]["status"] == "below"


def test_calibrate_seed_undefined_kappa_is_undecided(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> None:
    """Reference-only rows carry one label category: pe=1 leaves kappa undefined."""
    _write_seed_batch_config(env)
    _register_judges(cli_runner, args)
    _patch_seed_judges(monkeypatch, fake_server, "always-pass")
    gold = _make_seed_gold(cli_runner, env)
    kept = [
        line for line in gold.read_text(encoding="utf-8").splitlines() if "empty-agent" not in line
    ]
    reference_only = env.parent / "gold-reference-only.jsonl"
    reference_only.write_text("\n".join(kept) + "\n", encoding="utf-8")
    result = _calibrate_seed(cli_runner, args, env, reference_only)
    assert result.exit_code == 3, result.output
    assert "cohen_kappa for judge-a undefined" in result.output
    assert "gate not decided" in result.output
    assert "UNDECIDED" in result.output
    assert "PASS" not in result.output


def test_calibrate_seed_usage_errors(
    cli_runner: CliRunner, args: list[str], env: Path, tmp_path: Path
) -> None:
    """Mixed or missing modes and malformed seed rows are usage errors (exit 2)."""
    gold = tmp_path / "gold.jsonl"
    gold.write_text(
        json.dumps(
            {
                "sample_id": "logic-001/reference-a",
                "task_id": "logic-001",
                "item_id": "R6",
                "rubric_text": "q",
                "label": "pass",
                "evidence": "answers/reference-a",
                "seed_set": True,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    sheet = tmp_path / "sheet.jsonl"
    sheet.write_text(
        json.dumps({"trial_id": "t1", "item_id": "R6", "label": "pass"}) + "\n",
        encoding="utf-8",
    )
    # --seed with --labels or --batch-id: mutually exclusive.
    result = _calibrate_seed(cli_runner, args, env, gold, extra=["--labels", str(sheet)])
    assert result.exit_code == 2, result.output
    result = _calibrate_seed(cli_runner, args, env, gold, extra=["--batch-id", "2026-11-b1"])
    assert result.exit_code == 2, result.output
    # Neither mode.
    result = cli_runner.invoke(app, ["judges", "calibrate", *_calibrate_args(args)])
    assert result.exit_code == 2, result.output
    assert "calibrate needs --batch-id + --labels, or --seed" in result.output
    # The seed file cannot calibrate through the label-sheet mode either.
    result = cli_runner.invoke(
        app,
        [
            "judges",
            "calibrate",
            "--batch-id",
            "2026-11-b1",
            "--labels",
            str(gold),
            *_calibrate_args(args),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "trial_id and item_id are required" in result.output
    # Malformed seed rows.
    bad_label = tmp_path / "bad-label.jsonl"
    bad_label.write_text(
        json.dumps(
            {
                "sample_id": "logic-001/reference-a",
                "task_id": "logic-001",
                "item_id": "R6",
                "label": "maybe",
                "evidence": "answers/reference-a",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = _calibrate_seed(cli_runner, args, env, bad_label)
    assert result.exit_code == 2, result.output
    assert "label must be pass/fail" in result.output
    missing_evidence = tmp_path / "missing-evidence.jsonl"
    missing_evidence.write_text(
        json.dumps(
            {
                "sample_id": "logic-001/reference-a",
                "task_id": "logic-001",
                "item_id": "R6",
                "label": "pass",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = _calibrate_seed(cli_runner, args, env, missing_evidence)
    assert result.exit_code == 2, result.output
    assert "evidence is required" in result.output


def test_calibrate_seed_include_and_limit_bound_spend(
    cli_runner: CliRunner,
    args: list[str],
    env: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_server: FakeOpenAIServer,
) -> None:
    """--limit caps the judged rows; --include filters packs by glob."""
    _write_seed_batch_config(env)
    _register_judges(cli_runner, args)
    # always-fail vs the first sample's pass labels: percent 0.0, below the gate.
    _patch_seed_judges(monkeypatch, fake_server, "always-fail")
    gold = _make_seed_gold(cli_runner, env)
    result = _calibrate_seed(cli_runner, args, env, gold, extra=["--limit", "5"])
    assert result.exit_code == 1, result.output
    assert "5 rows judged by the panel" in result.output
    assert "judge spend $" in result.output

    # --include with no matching pack leaves nothing to judge.
    result = _calibrate_seed(
        cli_runner, args, env, gold, extra=["--include", "no-such-task-*", "--json"]
    )
    assert result.exit_code == 3, result.output
