"""Failure paths: preflight, crashes and teardown, hygiene, canary, leaks, manual review."""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from evalspleef.control.taskpack import TaskPack, load_task_pack
from evalspleef.control.trial import (
    Artifact,
    CanaryInjectionError,
    IsolationPreflightFailed,
    WorkspacePaths,
    classify_error,
)
from evalspleef.control.trial.prepare import stage_workspace
from evalspleef.control.trial.runner import _workspace_paths
from evalspleef.control.trial.testing import ScriptedEvaluator, make_result
from evalspleef.core.canary import CanaryRegistry
from evalspleef.core.feedback import (
    CallableLeakChecker,
    FeedbackDecision,
    LeakCategory,
    LeakViolation,
    RegexLeakChecker,
)
from evalspleef.spi.errors import (
    CapabilityNotSupported,
    EndpointMismatch,
    QuotaExceeded,
    SandboxCreateError,
    SandboxLost,
)
from evalspleef.spi.runner import ShellTurn
from evalspleef.testing.fake_gateway import TrialKeyRejected
from evalspleef.testing.scripted_runner import ScriptedRunner

FAILING = {"I2": "false"}
PASSING = {"I2": "true"}
TRIAL_ID = "backend-001.demo-model.scripted.e1.t01"


class CrashingRunner(ScriptedRunner):
    def __init__(self, error: BaseException) -> None:
        super().__init__()
        self._error = error

    async def send(self, message: str, *, deadline_s: int) -> ShellTurn:
        raise self._error


class RaisingEvaluator:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def evaluate(self, ctx: Any, round_no: int, final: bool) -> Any:
        raise self._error


# -- isolation preflight ---------------------------------------------------------------------


async def test_preflight_failure_retries_on_other_instances_then_fails_infra(
    harness_factory: Any, legal: Any
) -> None:
    h = await harness_factory(
        instances=("alpha", "beta", "gamma"),
        probes={"alpha": FAILING, "beta": FAILING, "gamma": FAILING},
        owns_terminal_events=True,
    )
    outcome = await h.run()

    assert outcome.status == "failed_infra"
    assert outcome.reason.startswith("IsolationPreflightFailed")
    assert [sorted(tried) for tried in h.replacer.calls] == [["alpha"], ["alpha", "beta"]]
    results = await h.of_type("preflight.result")
    assert [(e.payload["provider_instance"], e.payload["passed"]) for e in results] == [
        ("alpha", False),
        ("beta", False),
        ("gamma", False),
    ]
    assert results[0].payload["probes"][0]["invariant"] == "I2"
    assert results[0].payload["probes"][0]["exit_code"] == 1
    assert "trial.started" not in await h.types()
    assert h.scripted.instruction is None  # the agent never started
    # Standalone, the runner invalidates the failed record itself; a retry is a new trial.
    assert legal(await h.log()) == [
        ("fail_infra", "leased", "failed_infra"),
        ("invalidate", "failed_infra", "invalid"),
    ]
    assert all(box.destroyed for box in h.sandboxes())


async def test_preflight_failure_moves_the_trial_to_a_passing_instance(
    harness_factory: Any, rnd: Any
) -> None:
    h = await harness_factory(
        instances=("alpha", "beta"), probes={"alpha": FAILING, "beta": PASSING}, rounds=[rnd()]
    )
    outcome = await h.run()

    assert outcome.status == "scored"
    flow = outcome.record["extensions"]["flow"]
    assert (flow["provider_instance"], flow["agent_lease_id"]) == ("beta", "lease-agent-1")
    results = await h.of_type("preflight.result")
    assert [e.payload["passed"] for e in results] == [False, True]
    assert [e.payload["attempt"] for e in results] == [0, 1]
    assert h.first_lease.sandbox.destroyed  # the agent sandbox that failed is gone
    (beta,) = h.providers["beta"].created_sandboxes()
    assert beta.alive
    assert (beta.root / "workspace" / "solution.txt").exists()  # the agent worked on beta
    token = h.registry.token_of(h.trial.trial_id)
    assert token in (beta.root / "workspace" / "API.md").read_text()


async def test_preflight_failure_without_a_replacer_fails_at_once(harness_factory: Any) -> None:
    h = await harness_factory(
        instances=("alpha", "beta"), probes={"alpha": FAILING}, with_replacer=False
    )
    outcome = await h.run()
    assert outcome.status == "failed_infra"
    assert len(await h.of_type("preflight.result")) == 1


# -- crashes and teardown ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status", "path"),
    [
        (
            SandboxLost("the host vanished"),
            "failed_infra",
            [("fail_infra", "running", "failed_infra"), ("invalidate", "failed_infra", "invalid")],
        ),
        (
            RuntimeError("a bug in the shell"),
            "invalid",
            [("fail_infra", "running", "failed_infra"), ("invalidate", "failed_infra", "invalid")],
        ),
        (
            CapabilityNotSupported("not offered"),
            "invalid",
            [("fail_infra", "running", "failed_infra"), ("invalidate", "failed_infra", "invalid")],
        ),
    ],
)
async def test_runner_crash_still_revokes_the_key_and_tears_down(
    harness_factory: Any, legal: Any, error: Exception, status: str, path: list[tuple[str, ...]]
) -> None:
    crashing = CrashingRunner(error)
    h = await harness_factory(scripted=crashing, owns_terminal_events=True)
    outcome = await h.run()

    assert outcome.status == status
    assert outcome.reason.startswith(type(error).__name__)
    assert legal(await h.log())[1:] == path
    with pytest.raises(TrialKeyRejected, match="revoked"):
        h.gateway.authenticate(crashing.trial_key)
    with pytest.raises(RuntimeError, match="stopped"):
        await ScriptedRunner.send(crashing, "again", deadline_s=1)
    assert all(box.destroyed for box in h.sandboxes())
    types = await h.types()
    assert types[-3:] == ["trial.invalidated", "teardown.started", "teardown.completed"]
    incident = (await h.of_type("incident.opened"))[0]
    assert (incident.payload["kind"], incident.payload["outcome"]) == ("trial_error", status)


async def test_evaluator_failure_while_observing_is_mapped_and_torn_down(
    harness_factory: Any, rnd: Any, legal: Any
) -> None:
    h = await harness_factory(rounds=[rnd()], evaluator=RaisingEvaluator(SandboxLost("gone")))
    outcome = await h.run()

    assert outcome.status == "failed_infra"
    # observing has no failed_infra transition in the table: the log stops at the last legal one
    assert legal(await h.log()) == [
        ("start", "leased", "running"),
        ("stop", "running", "observing"),
    ]
    with pytest.raises(TrialKeyRejected, match="revoked"):
        h.gateway.authenticate(h.scripted.trial_key)


# -- identity hygiene ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "named"),
    [
        ({"TASK_ID": "x"}, "TASK_ID"),
        ({"API_TOKEN": "sk-abcdefghijkl"}, "API_TOKEN"),
        ({"NOTE": "this is backend-001"}, "NOTE"),
    ],
)
async def test_identity_hygiene_violation_invalidates_the_trial(
    harness_factory: Any, legal: Any, env: dict[str, str], named: str
) -> None:
    h = await harness_factory(agent_env=env)
    outcome = await h.run()

    assert outcome.status == "invalid"
    assert named in outcome.reason
    assert "sk-abcdefghijkl" not in outcome.reason
    types = await h.types()
    assert "preflight.result" not in types and "trial.started" not in types
    assert legal(await h.log()) == [
        ("fail_infra", "leased", "failed_infra"),
        ("invalidate", "failed_infra", "invalid"),
    ]


# -- canary -------------------------------------------------------------------------------------


async def test_canary_is_written_into_the_files_named_by_the_pack(
    harness_factory: Any, rnd: Any
) -> None:
    h = await harness_factory(rounds=[rnd()])
    await h.run()

    token = h.registry.token_of(h.trial.trial_id)
    workspace = h.first_lease.sandbox.root / "workspace"
    (event,) = await h.of_type("canary.injected")
    hashes = {item["path"]: item["sha256"] for item in event.payload["placements"]}
    assert set(hashes) == {"API.md", "ordersvc/__init__.py", "NOTICE.txt"}
    for relative, digest in hashes.items():
        content = (workspace / relative).read_bytes()
        assert token.encode() in content
        assert hashlib.sha256(content).hexdigest() == digest
    protected = [p for p in (workspace / "tests").rglob("*") if p.is_file()]
    assert protected
    assert all(token.encode() not in p.read_bytes() for p in protected)
    assert all(token not in event.to_json_line() for event in await h.log())


async def test_foreign_canary_in_an_artifact_invalidates_the_trial_after_scoring(
    harness_factory: Any, rnd: Any, legal: Any, tmp_path: Path
) -> None:
    registry = CanaryRegistry()
    other = registry.register("backend-001.other-model.scripted.e1.t01")
    leaked = tmp_path / "out.txt"
    leaked.write_text(f"copied from somewhere: {other}\n")
    final = make_result(artifacts=[Artifact("out.txt", "0" * 64, 12, leaked)])
    h = await harness_factory(
        rounds=[rnd()], final=final, registry=registry, owns_terminal_events=True
    )
    outcome = await h.run()

    assert outcome.status == "invalid"
    assert outcome.record["state"] == "invalid"
    assert legal(await h.log())[-2:] == [
        ("score", "verifying", "scored"),
        ("invalidate_post_hoc", "scored", "invalid"),
    ]
    types = await h.types()
    assert types.index("grade.computed") < types.index("canary.scanned")
    assert "trial.invalidated" in types and "trial.scored" not in types
    incident = (await h.of_type("incident.opened"))[0]
    assert incident.payload["kind"] == "canary_cross_trial"
    assert all(other not in event.to_json_line() for event in await h.log())


async def test_the_trials_own_canary_in_an_artifact_is_fine(
    harness_factory: Any, rnd: Any, tmp_path: Path
) -> None:
    registry = CanaryRegistry()
    own = registry.register(TRIAL_ID)
    kept = tmp_path / "out.txt"
    kept.write_text(f"{own}\n")
    final = make_result(artifacts=[Artifact("out.txt", "0" * 64, 12, kept)])
    h = await harness_factory(rounds=[rnd()], final=final, registry=registry)
    outcome = await h.run()
    assert outcome.status == "scored"


@pytest.fixture
def prose_pack(pack: TaskPack, tmp_path: Path) -> TaskPack:
    """The backend pack with a canary manifest that names no file."""
    root = tmp_path / "pack" / pack.task_id
    shutil.copytree(pack.root, root)
    (root / "canary.md").write_text("# canary\n\nThe token goes somewhere sensible.\n")
    return load_task_pack(root)


TOKEN = "CNRY-" + "0" * 8 + "-" + "1" * 8


def test_canary_without_a_machine_readable_manifest_goes_into_the_first_entry_file(
    prose_pack: TaskPack, tmp_path: Path
) -> None:
    staged = stage_workspace(
        pack=prose_pack,
        workspace=_workspace_paths(prose_pack),
        trial_id=TRIAL_ID,
        token=TOKEN,
        scratch=tmp_path / "scratch",
    )
    assert [(p.path, p.kind) for p in staged.placements] == [("README.md", "comment")]
    assert TOKEN in (staged.directory / "README.md").read_text()
    assert staged.hashes == {
        "README.md": hashlib.sha256((staged.directory / "README.md").read_bytes()).hexdigest()
    }


def test_canary_injection_fails_when_no_file_can_carry_it(
    prose_pack: TaskPack, tmp_path: Path
) -> None:
    with pytest.raises(CanaryInjectionError):
        stage_workspace(
            pack=prose_pack,
            workspace=WorkspacePaths("/workspace", None, ()),
            trial_id=TRIAL_ID,
            token=TOKEN,
            scratch=tmp_path / "scratch",
        )


# -- feedback leaks --------------------------------------------------------------------------------


class TwoSentenceWriter:
    """A writer whose first draft leaks; its rewrite drops the leaking sentence."""

    def __init__(self, draft: str = "第一句没问题。LEAKWORD 第二句。") -> None:
        self._draft = draft

    def compose(self, decision: FeedbackDecision) -> str:
        return self._draft

    def rewrite(self, text: str, violations: Any) -> str:
        return "第一句没问题。"

    def nudge(self, index: int) -> str:
        return "继续。"

    def answer(self, question: str) -> str:
        return "好的。"


async def test_a_leaking_draft_is_rewritten_and_then_sent(
    harness_factory: Any, rnd: Any, blocking: str
) -> None:
    h = await harness_factory(
        observations=[make_result(failing=[blocking]), make_result()],
        rounds=[rnd(), rnd()],
        leak_checker=RegexLeakChecker(forbidden_terms=["LEAKWORD"]),
        writer_factory=lambda pack: TwoSentenceWriter(),
    )
    outcome = await h.run()

    assert h.scripted.received_messages[1] == "第一句没问题。"
    (record,) = outcome.record["extensions"]["flow"]["feedback"]
    assert record["leak_check"]["rewrite_count"] == 1
    assert record["leak_check"]["final_action"] == "send"
    assert record["delivery"]["status"] == "sent"
    assert outcome.record["grades"][0]["tier"] == "B"


async def test_three_failed_rewrites_block_the_feedback(
    harness_factory: Any, rnd: Any, blocking: str, legal: Any
) -> None:
    always = CallableLeakChecker(
        lambda text, level: [LeakViolation(LeakCategory.OTHER, "always flagged")]
    )
    h = await harness_factory(
        observations=[make_result(failing=[blocking])],
        rounds=[rnd(), rnd()],
        leak_checker=always,
    )
    outcome = await h.run()

    grade = outcome.record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("F", "G-ABORT-04")
    assert "feedback_blocked" in grade["flags"]
    (record,) = outcome.record["extensions"]["flow"]["feedback"]
    assert (record["delivery"]["status"], record["leak_check"]["rewrite_count"]) == ("blocked", 3)
    assert len(h.scripted.received_messages) == 1
    types = await h.types()
    assert "feedback.sent" not in types and "verify.started" in types
    assert ("feedback_blocked", "awaiting_feedback", "verifying") in legal(await h.log())


async def test_missing_feedback_material_ends_the_trial(
    harness_factory: Any, rnd: Any, blocking: str, legal: Any
) -> None:
    h = await harness_factory(
        observations=[make_result(failing=[blocking])],
        rounds=[rnd(), rnd()],
        writer_factory=lambda pack: TwoSentenceWriter(draft=""),
    )
    outcome = await h.run()

    grade = outcome.record["grades"][0]
    assert (grade["tier"], grade["decision"]["rule_id"]) == ("F", "G-ABORT-04")
    assert ("finish", "awaiting_feedback", "verifying") in legal(await h.log())


# -- manual review and verifier isolation ------------------------------------------------------------


@pytest.mark.parametrize(
    "final",
    [
        make_result(manual_reasons=["the judge returned no usable verdict"]),
        make_result(inconclusive=["R1"]),
        make_result(gates_pass=False),
    ],
    ids=["unusable-judge", "inconclusive-item", "observer-and-verifier-disagree"],
)
async def test_unstable_verification_pauses_for_a_human(
    harness_factory: Any, rnd: Any, legal: Any, final: Any
) -> None:
    h = await harness_factory(rounds=[rnd()], final=final)
    outcome = await h.run()

    assert outcome.status == "paused_manual"
    assert outcome.reason
    record = outcome.record
    assert (record["state"], record["validity"], record["grades"]) == (
        "paused_manual",
        "manual",
        [],
    )
    assert legal(await h.log())[-1] == ("pause", "verifying", "paused_manual")


async def test_a_verifier_sharing_the_agent_lease_invalidates_the_trial(
    harness_factory: Any, rnd: Any
) -> None:
    final = replace(make_result(), verifier_lease_id="lease-agent-0")
    h = await harness_factory(
        rounds=[rnd()], evaluator=ScriptedEvaluator([make_result()], final, open_verifier=False)
    )
    outcome = await h.run()
    assert outcome.status == "invalid"
    assert outcome.reason.startswith("LeaseInvariantError")


# -- error mapping ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (SandboxLost("gone"), "failed_infra"),
        (QuotaExceeded("full"), "failed_infra"),
        (SandboxCreateError("image"), "failed_infra"),
        (IsolationPreflightFailed("probe"), "failed_infra"),
        (EndpointMismatch("other vendor"), "invalid"),
        (CapabilityNotSupported("gpu"), "invalid"),
        (CanaryInjectionError("no file"), "invalid"),
        (RuntimeError("unknown"), "invalid"),
    ],
)
def test_errors_map_to_failed_infra_or_invalid(error: Exception, status: str) -> None:
    assert classify_error(error).status == status


def test_a_subclass_of_a_taxonomy_error_maps_like_its_parent() -> None:
    class HostVanished(SandboxLost):
        pass

    verdict = classify_error(HostVanished("x"))
    assert (verdict.status, verdict.error_class, verdict.disposition) == (
        "failed_infra",
        "environment",
        "discard",
    )
