"""Judge client over the gateway chat endpoint and the JudgePanel wiring."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from evalspleef.control.observe.artifacts import WorkspaceExport
from evalspleef.control.observe.judging import (
    GatewayJudgeClient,
    JudgeStep,
    evidence_digest,
)
from evalspleef.control.taskpack import TaskPack
from evalspleef.control.trial import classify_error
from evalspleef.control.trial.types import Artifact
from evalspleef.core.judge import JudgeConfigError, MockJudge
from evalspleef.spi.errors import EndpointUnavailable
from evalspleef.spi.gateway import COST_HEADER, PriceSpec


class _Recorder:
    def __init__(self, replies: list[dict[str, object]]) -> None:
        self.requests: list[httpx.Request] = []
        self._replies = replies

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = {
            "choices": [
                {"message": {"content": json.dumps({"verdict": True})}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        if self._replies:
            body.update(self._replies.pop(0))
        return httpx.Response(200, json=body)


def test_gateway_client_posts_chat_completion() -> None:
    recorder = _Recorder([])
    client = GatewayJudgeClient(
        endpoint_id="ep_judge-1",
        gateway_url="http://gw.test/v1",
        provider="vendor-a",
        family="fam-a",
        api_key="secret-key",
        transport=httpx.MockTransport(recorder.handler),
    )
    out = client.complete("hello judge")
    assert json.loads(out)["verdict"] is True
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret-key"
    payload = json.loads(request.content)
    assert payload["model"] == "ep_judge-1"
    assert payload["messages"] == [{"role": "user", "content": "hello judge"}]  # one-shot
    client.complete("again")
    assert client.total_input_tokens == 20 and client.total_output_tokens == 10


def _judge_client(handler: object) -> GatewayJudgeClient:
    return GatewayJudgeClient(
        endpoint_id="ep_judge-1",
        gateway_url="http://gw.test/v1",
        provider="vendor-a",
        family="fam-a",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


def test_gateway_client_and_step_book_the_gateway_charge(pack_factory, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [{"message": {"content": json.dumps({"verdict": True})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return httpx.Response(200, json=body, headers={COST_HEADER: "0.25"})

    client = _judge_client(handler)
    client.complete("x")
    client.complete("y")
    assert client.total_cost_usd == pytest.approx(0.5)

    pack: TaskPack = pack_factory("logic-001")
    export = WorkspaceExport(tree=tmp_path, artifacts=(), issues=(), file_count=0, total_bytes=0)
    judge_items = len([item for item in pack.rubric.items if item.kind.value == "llm_judge"])
    charged = _judge_client(handler)
    step = JudgeStep({"vendor-a": charged}, target_family="fam-target", allow_single_vendor=True)
    run = step.run(pack, export, scratch_dir=tmp_path / "judge")
    # No price table: the run costs what the gateway charged for its 5 samples per item.
    assert run.cost_usd == pytest.approx(judge_items * 5 * 0.25)


def test_a_shared_client_books_each_run_only_its_own_charge(pack_factory, tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "choices": [{"message": {"content": json.dumps({"verdict": True})}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return httpx.Response(200, json=body, headers={COST_HEADER: "0.25"})

    pack: TaskPack = pack_factory("logic-001")
    export = WorkspaceExport(tree=tmp_path, artifacts=(), issues=(), file_count=0, total_bytes=0)
    judge_items = len([item for item in pack.rubric.items if item.kind.value == "llm_judge"])
    step = JudgeStep(
        {"vendor-a": _judge_client(handler)}, target_family="fam-target", allow_single_vendor=True
    )
    first = step.run(pack, export, scratch_dir=tmp_path / "judge-1")
    second = step.run(pack, export, scratch_dir=tmp_path / "judge-2")
    assert first.cost_usd == pytest.approx(judge_items * 5 * 0.25)
    assert second.cost_usd == pytest.approx(first.cost_usd)


def test_gateway_client_raises_on_client_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "bad"}})

    with pytest.raises(httpx.HTTPStatusError):
        _judge_client(handler).complete("x")


@pytest.mark.parametrize(
    "reply",
    [
        lambda request: httpx.Response(500, json={"error": {"message": "boom"}}),
        lambda request: httpx.Response(504, json={"error": {"code": "upstream_timeout"}}),
        lambda request: httpx.Response(429, json={"error": {"message": "slow down"}}),
    ],
)
def test_gateway_server_errors_are_retryable_infrastructure(reply: object) -> None:
    with pytest.raises(EndpointUnavailable) as info:
        _judge_client(reply).complete("x")
    assert classify_error(info.value).status == "failed_infra"


def test_judge_read_timeout_is_retryable_infrastructure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("judge too slow", request=request)

    with pytest.raises(EndpointUnavailable) as info:
        _judge_client(handler).complete("x")
    assert classify_error(info.value).status == "failed_infra"


def test_evidence_digest_uses_sanitised_artifacts_only(tmp_path: Path) -> None:
    artifact_file = tmp_path / "answer.json"
    artifact_file.write_text('{"instances": []}', encoding="utf-8")
    export = WorkspaceExport(
        tree=tmp_path,
        artifacts=(
            Artifact(
                path="answer.json",
                sha256=hashlib.sha256(b'{"instances": []}').hexdigest(),
                size_bytes=17,
                local_path=artifact_file,
            ),
        ),
        issues=(),
        file_count=1,
        total_bytes=17,
    )
    digest = evidence_digest(export)
    assert "answer.json" in digest
    assert "sha256=" in digest
    assert '{"instances": []}' in digest


def _two_vendors(pass_a: bool = True, pass_b: bool = True) -> dict[str, MockJudge]:
    return {
        "vendor-a": MockJudge(verdicts=[pass_a], provider="vendor-a", family="fam-a"),
        "vendor-b": MockJudge(verdicts=[pass_b], provider="vendor-b", family="fam-b"),
    }


def test_judge_step_runs_panel_and_snapshots(pack_factory, tmp_path: Path) -> None:
    pack: TaskPack = pack_factory("logic-001")
    export = WorkspaceExport(tree=tmp_path, artifacts=(), issues=(), file_count=0, total_bytes=0)
    step = JudgeStep(_two_vendors(), target_family="fam-target")
    run = step.run(pack, export, scratch_dir=tmp_path / "judge")

    judge_items = [item for item in pack.rubric.items if item.kind.value == "llm_judge"]
    assert {r.item_id for r in run.results} == {item.id for item in judge_items}
    assert all(r.verdict == "pass" for r in run.results)
    # k=5 samples for every item, two vendors
    assert run.samples == 2 * 5
    assert run.agreement == 1.0
    # snapshot exists and its hash is recorded
    assert run.snapshot_path.is_file()
    assert run.snapshot_sha256 == hashlib.sha256(run.snapshot_path.read_bytes()).hexdigest()
    # the snapshot carries the session id and the evidence digest section
    text = run.snapshot_path.read_text(encoding="utf-8")
    assert f"judge_session: {run.session_id}" in text


def test_judge_step_isolated_sessions_per_run(pack_factory, tmp_path: Path) -> None:
    pack: TaskPack = pack_factory("logic-001")
    export = WorkspaceExport(tree=tmp_path, artifacts=(), issues=(), file_count=0, total_bytes=0)
    step = JudgeStep(_two_vendors(), target_family="fam-target")
    first = step.run(pack, export, scratch_dir=tmp_path / "j1")
    second = step.run(pack, export, scratch_dir=tmp_path / "j2")
    assert first.session_id != second.session_id
    assert first.snapshot_path != second.snapshot_path


def test_judge_step_disagreement_goes_inconclusive_and_manual(pack_factory, tmp_path: Path) -> None:
    pack: TaskPack = pack_factory("logic-001")
    export = WorkspaceExport(tree=tmp_path, artifacts=(), issues=(), file_count=0, total_bytes=0)
    step = JudgeStep(_two_vendors(pass_a=True, pass_b=False), target_family="fam-target")
    run = step.run(pack, export, scratch_dir=tmp_path / "judge")
    assert all(r.verdict == "inconclusive" for r in run.results)
    assert len(run.manual_queue) == len(run.results)


def test_judge_step_family_rule_rejects_same_family(pack_factory, tmp_path: Path) -> None:
    pack: TaskPack = pack_factory("logic-001")
    export = WorkspaceExport(tree=tmp_path, artifacts=(), issues=(), file_count=0, total_bytes=0)
    step = JudgeStep(_two_vendors(), target_family="fam-a")
    with pytest.raises(JudgeConfigError):
        step.run(pack, export, scratch_dir=tmp_path / "judge")


class _TokenJudge:
    """JudgeClient stand-in that reports deterministic token usage."""

    def __init__(self, name: str, family: str, verdict: bool) -> None:
        self.name = name
        self.provider = name
        self.family = family
        self._verdict = verdict
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    def complete(self, prompt: str) -> str:
        self.total_input_tokens += 100
        self.total_output_tokens += 50
        return json.dumps({"verdict": self._verdict})


def test_judge_step_costs_tokens_with_prices(pack_factory, tmp_path: Path) -> None:
    pack: TaskPack = pack_factory("logic-001")
    export = WorkspaceExport(tree=tmp_path, artifacts=(), issues=(), file_count=0, total_bytes=0)
    prices = {
        "vendor-a": PriceSpec(input_per_mtok=2.0, output_per_mtok=6.0),
        "vendor-b": PriceSpec(input_per_mtok=1.0, output_per_mtok=3.0),
    }
    clients = {
        "vendor-a": _TokenJudge("judge-a", "fam-a", True),
        "vendor-b": _TokenJudge("judge-b", "fam-b", True),
    }
    judge_items = len([item for item in pack.rubric.items if item.kind.value == "llm_judge"])
    step = JudgeStep(clients, target_family="fam-target", prices=prices)
    run = step.run(pack, export, scratch_dir=tmp_path / "judge")
    # per item and vendor: 5 samples x (100 in / 50 out); both vendors' rates apply.
    per_item_usd = 5 * (100 * (2.0 + 1.0) + 50 * (6.0 + 3.0)) / 1_000_000
    assert run.cost_usd == pytest.approx(judge_items * per_item_usd)
    assert run.samples == 2 * 5
