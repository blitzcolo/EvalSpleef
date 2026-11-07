"""Unit tests for the conformance report model and writer."""

from __future__ import annotations

import json

from evalspleef.conformance.report import CaseResult, ConformanceReport, TargetSummary


def _report() -> ConformanceReport:
    results = (
        CaseResult(
            target="mock-sandbox",
            case_id="C-LIFE-01",
            title="create, exec, destroy",
            status="pass",
            detail="",
            fake=True,
        ),
        CaseResult(
            target="mock-sandbox",
            case_id="C-GUI-01",
            title="gui endpoint",
            status="not-applicable",
            detail="gui not declared",
            fake=True,
        ),
        CaseResult(
            target="real-sandbox",
            case_id="C-LIFE-01",
            title="create, exec, destroy",
            status="fail",
            detail="exit code was 1",
            fake=False,
        ),
    )
    summaries = (
        TargetSummary(
            name="mock-sandbox",
            kind="sandbox",
            provider="MockSandboxProvider",
            fake=True,
            notes="double",
            passed=1,
            failed=0,
            not_applicable=1,
        ),
        TargetSummary(
            name="real-sandbox",
            kind="sandbox",
            provider="DockerSandboxProvider",
            fake=False,
            notes="",
            passed=0,
            failed=1,
            not_applicable=0,
        ),
    )
    return ConformanceReport(targets=summaries, results=results)


def test_counts_and_dict_shape() -> None:
    report = _report()
    assert report.counts() == {"pass": 1, "fail": 1, "not-applicable": 1}
    data = report.to_dict()
    assert data["counts"] == {"pass": 1, "fail": 1, "not-applicable": 1}
    assert [target["name"] for target in data["targets"]] == ["mock-sandbox", "real-sandbox"]
    assert data["targets"][0]["fake"] is True
    assert data["targets"][1]["fake"] is False
    assert len(data["results"]) == 3
    statuses = {(entry["target"], entry["case_id"]): entry["status"] for entry in data["results"]}
    assert statuses == {
        ("mock-sandbox", "C-LIFE-01"): "pass",
        ("mock-sandbox", "C-GUI-01"): "not-applicable",
        ("real-sandbox", "C-LIFE-01"): "fail",
    }


def test_json_round_trip() -> None:
    report = _report()
    parsed = json.loads(report.to_json())
    assert parsed["results"][1]["detail"] == "gui not declared"
    assert parsed["results"][2]["fake"] is False


def test_markdown_lists_every_case_with_status_and_fake_flag() -> None:
    markdown = _report().to_markdown()
    assert "| Target | Case | Title | Status | Fake | Detail |" in markdown
    assert "mock-sandbox | C-LIFE-01" in markdown
    assert "not-applicable" in markdown
    assert "exit code was 1" in markdown
    assert "| real-sandbox | sandbox | DockerSandboxProvider | False |" in markdown
    assert "| mock-sandbox | sandbox | MockSandboxProvider | True |" in markdown


def test_write_emits_json_and_markdown(tmp_path) -> None:
    report = _report()
    json_path, markdown_path = report.write(tmp_path / "out" / "report.json")
    assert json_path == tmp_path / "out" / "report.json"
    assert markdown_path == tmp_path / "out" / "report.md"
    parsed = json.loads(json_path.read_text(encoding="utf-8"))
    assert parsed["counts"]["fail"] == 1
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "C-GUI-01" in markdown


def test_write_with_markdown_suffix(tmp_path) -> None:
    json_path, markdown_path = _report().write(tmp_path / "report.md")
    assert json_path == tmp_path / "report.json"
    assert markdown_path == tmp_path / "report.md"
    assert json_path.exists()
    assert markdown_path.exists()


def test_empty_report() -> None:
    report = ConformanceReport(targets=())
    assert report.counts() == {"pass": 0, "fail": 0, "not-applicable": 0}
    assert "## Cases" in report.to_markdown()
