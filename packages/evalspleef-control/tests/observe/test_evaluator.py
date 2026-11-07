"""Evaluator wiring: pipeline assembly, cheat signals, host-tools marking, regrade mode."""

from __future__ import annotations

from evalspleef.control.observe import Evaluator
from evalspleef.core.judge import MockJudge
from evalspleef.testing.mock_sandbox import MockSandbox


def _stage_logic_workspace(fixture_name: str, hmod):
    """Stage the agent sandbox /workspace from a fixture's embedded files."""

    async def stage(sandbox: MockSandbox) -> None:
        fixture = hmod.load_fixture(fixture_name)
        files = fixture["files"]
        assert isinstance(files, dict)
        staging = sandbox.root / "staging"
        for rel, content in files.items():
            local = staging / rel
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text(str(content), encoding="utf-8")
            await sandbox.upload(local, f"/workspace/{rel}")

    return stage


async def test_evaluate_reference_end_to_end(pack_factory, make_trial_ctx, tmp_path, hmod) -> None:
    """One evaluation: export -> verify -> map, all against real mock sandboxes."""
    pack = pack_factory("logic-001")
    ctx, _agent = await make_trial_ctx(
        pack, tmp_path, stage_agent=_stage_logic_workspace("logic-001.reference", hmod)
    )
    evaluator = Evaluator()
    result = await evaluator.evaluate(ctx, 1, final=True)

    assert result.hard_gates_passed
    assert {gate.check_id for gate in result.hard_gates} == {"HG1", "HG2", "HG3"}
    assert result.verifier_lease_id == "lease-verifier-1"
    assert result.verifier_lease_id != ctx.agent.lease_id  # I10
    assert result.cheat_signals == ()

    artifacts = {artifact.path: artifact for artifact in result.artifacts}
    assert set(artifacts) == {"answer.json", "instances.json"}
    assert all(artifact.sha256 for artifact in artifacts.values())
    assert artifacts["answer.json"].local_path is not None
    assert artifacts["answer.json"].local_path.is_file()

    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R1"] == "pass"
    assert verdicts["R2"] == "pass"
    assert verdicts["R4"] == "pass"
    # no judge panel configured: llm_judge items are inconclusive and flagged
    assert verdicts["R6"] == "inconclusive"
    assert verdicts["R7"] == "inconclusive"
    assert any("no judge panel" in reason for reason in result.manual_reasons)
    assert result.judge_samples == 0

    # the verifier sandbox really ran the pack's own hidden tests
    verifier = ctx.leases.verifier
    assert verifier is not None
    found = await verifier.sandbox.exec(
        "test -f hidden-tests/verify_submission.py && echo yes", cwd="/verify", timeout_s=30.0
    )
    assert found.stdout.decode().strip() == "yes"


async def test_evaluate_flags_tampering_as_cheat(
    pack_factory, make_trial_ctx, tmp_path, hmod
) -> None:
    pack = pack_factory("logic-001")
    ctx, _agent = await make_trial_ctx(
        pack,
        tmp_path,
        stage_agent=_stage_logic_workspace("logic-001.tampered-input", hmod),
    )
    result = await Evaluator().evaluate(ctx, 1, final=True)
    assert not result.hard_gates_passed
    gates = {gate.check_id: gate.passed for gate in result.hard_gates}
    assert gates == {"HG1": True, "HG2": True, "HG3": False}
    kinds = {signal.kind for signal in result.cheat_signals}
    assert "hash_lock" in kinds
    assert any("instances.json" in signal.detail for signal in result.cheat_signals)
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R4"] == "fail"


async def test_evaluate_with_judges(pack_factory, make_trial_ctx, tmp_path, hmod) -> None:
    pack = pack_factory("logic-001")
    ctx, _agent = await make_trial_ctx(
        pack, tmp_path, stage_agent=_stage_logic_workspace("logic-001.reference", hmod)
    )
    judges = {
        "vendor-a": MockJudge(verdicts=[True], provider="vendor-a", family="fam-a"),
        "vendor-b": MockJudge(verdicts=[True], provider="vendor-b", family="fam-b"),
    }
    evaluator = Evaluator(judges=judges, target_family="fam-target", judge_k=1)
    result = await evaluator.evaluate(ctx, 1, final=True)
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R6"] == "pass"
    assert verdicts["R7"] == "pass"
    assert result.judge_samples == 2
    assert result.judge_consistency == 1.0
    assert not result.manual_reasons


async def test_host_tools_packs_pause_for_human(
    pack_factory, make_trial_ctx, tmp_path, hmod
) -> None:
    """macOS/GPU/ffmpeg packs report manual reasons instead of faking gate results."""
    pack = pack_factory("logic-001")
    ctx, _agent = await make_trial_ctx(pack, tmp_path)
    evaluator = Evaluator(host_tools=lambda pack: "needs a host toolchain")
    result = await evaluator.evaluate(ctx, 1, final=True)
    assert result.hard_gates == ()
    assert result.manual_reasons == ("logic-001: needs a host toolchain",)
    assert ctx.leases.verifier is None  # no verifier sandbox was opened


async def test_for_regrade_uses_stored_artifacts(
    pack_factory, make_trial_ctx, tmp_path, hmod
) -> None:
    """verify_only mode: regrade a stored export without touching the agent sandbox."""
    pack = pack_factory("logic-001")
    ctx, agent = await make_trial_ctx(pack, tmp_path)
    stored = hmod.fixture_workspace(pack, tmp_path, "logic-001.reference")
    evaluator = Evaluator.for_regrade(stored)
    result = await evaluator.evaluate(ctx, 1, final=True)
    assert result.hard_gates_passed

    # the agent sandbox was never touched: destroy it and regrade again
    for sandbox in [
        agent,
        *(s for s in [ctx.leases.verifier.sandbox if ctx.leases.verifier else None] if s),
    ]:
        await sandbox.destroy()
    ctx2, agent2 = await make_trial_ctx(pack, tmp_path / "second")
    await agent2.destroy()
    evaluator2 = Evaluator.for_regrade(stored)
    result2 = await evaluator2.evaluate(ctx2, 1, final=True)
    assert result2.hard_gates_passed


async def test_empty_agent_fails_hard_gates(pack_factory, make_trial_ctx, tmp_path, hmod) -> None:
    pack = pack_factory("logic-001")
    ctx, _agent = await make_trial_ctx(
        pack, tmp_path, stage_agent=_stage_logic_workspace("logic-001.empty", hmod)
    )
    result = await Evaluator().evaluate(ctx, 1, final=True)
    assert not result.hard_gates_passed
    verdicts = {item.item_id: item.verdict for item in result.rubric}
    assert verdicts["R1"] == "fail"
    assert verdicts["R2"] == "fail"
    assert verdicts["R4"] == "pass"
