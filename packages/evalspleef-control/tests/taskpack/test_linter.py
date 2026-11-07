"""Linter rule tests plus a lint pass over the ten real packs in ``tasks/``."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from evalspleef.control.taskpack import LintReport, lint_task_pack_dir, load_task_pack
from evalspleef.control.taskpack.linter import PIN_ON_HOST_MARKER

REPO_ROOT = Path(__file__).resolve().parents[4]
TASKS_DIR = REPO_ROOT / "tasks"


def _rules(report: LintReport) -> list[str]:
    return [issue.rule for issue in report.issues]


def test_valid_fixture_pack_passes(pack_factory: Callable[[str], Path]) -> None:
    report = lint_task_pack_dir(pack_factory())
    assert report.ok, [str(issue) for issue in report.errors]
    assert report.errors == []


def test_missing_required_files(pack_factory: Callable[[str], Path]) -> None:
    pack_dir = pack_factory()
    (pack_dir / "notes.md").unlink()
    (pack_dir / "hidden-tests" / "run.sh").unlink()
    report = lint_task_pack_dir(pack_dir)
    assert not report.ok
    assert _rules(report).count("required-files") == 2


def test_missing_reference_solution(pack_factory: Callable[[str], Path]) -> None:
    pack_dir = pack_factory()
    (pack_dir / "answers" / "reference-b.py").unlink()
    report = lint_task_pack_dir(pack_dir)
    assert "required-files" in _rules(report)
    assert not report.ok


def test_missing_runtime_requirements_is_error(
    pack_factory: Callable[[str], Path], task_yaml_template: str
) -> None:
    report = lint_task_pack_dir(
        pack_factory(task_yaml_template.replace("  requirements:\n", "  other:\n"))
    )
    assert "runtime-requirements" in _rules(report)
    assert not report.ok


def test_unpinned_image_is_error(
    pack_factory: Callable[[str], Path], task_yaml_template: str
) -> None:
    report = lint_task_pack_dir(
        pack_factory(
            task_yaml_template.replace(
                'oci_image: "llm-eval/test@sha256:abc123"',
                'oci_image: "llm-eval/test:latest"',
            )
        )
    )
    assert "image-digest" in _rules(report)
    assert not report.ok


def test_pin_on_host_marker_is_warning_not_error(
    pack_factory: Callable[[str], Path], task_yaml_template: str
) -> None:
    report = lint_task_pack_dir(
        pack_factory(
            task_yaml_template.replace(
                'oci_image: "llm-eval/test@sha256:abc123"',
                f'oci_image: "llm-eval/test@{PIN_ON_HOST_MARKER}"',
            )
        )
    )
    assert report.ok
    assert [issue.rule for issue in report.warnings] == ["image-digest-pending"]


def test_lifetime_rule(pack_factory: Callable[[str], Path], task_yaml_template: str) -> None:
    report = lint_task_pack_dir(
        pack_factory(task_yaml_template.replace("max_lifetime_s: 7200", "max_lifetime_s: 7199"))
    )
    assert "lifetime" in _rules(report)
    assert not report.ok
    assert "7199" in report.errors[0].message
    assert "7200" in report.errors[0].message


def test_in_sandbox_requires_gateway_egress(
    pack_factory: Callable[[str], Path], task_yaml_template: str
) -> None:
    report = lint_task_pack_dir(
        pack_factory(
            task_yaml_template.replace(
                "execution_modes: [remote-tools]", "execution_modes: [in-sandbox]"
            )
        )
    )
    assert "gateway-egress" in _rules(report)
    assert not report.ok

    # declaring a non-off network mode satisfies the rule
    report2 = lint_task_pack_dir(
        pack_factory(
            task_yaml_template.replace(
                "execution_modes: [remote-tools]", "execution_modes: [in-sandbox]"
            ).replace('network: "off"', "network: docs_whitelist")
        )
    )
    assert "gateway-egress" not in _rules(report2)
    assert report2.ok


def test_hidden_tests_inside_starter_is_error(pack_factory: Callable[[str], Path]) -> None:
    pack_dir = pack_factory()
    leaked = pack_dir / "workspace" / "hidden-tests"
    leaked.mkdir()
    (leaked / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    report = lint_task_pack_dir(pack_dir)
    assert "hidden-tests-isolation" in _rules(report)
    assert not report.ok


def test_defects_link_rule(pack_factory: Callable[[str], Path], defects_yaml_template: str) -> None:
    pack_dir = pack_factory()
    (pack_dir / "defects.yaml").write_text(
        defects_yaml_template.replace("rubric_item_id: R2", "rubric_item_id: R42"),
        encoding="utf-8",
    )
    report = lint_task_pack_dir(pack_dir)
    assert "defects-link" in _rules(report)
    assert not report.ok


def test_rubric_f1_symptom_rule(
    pack_factory: Callable[[str], Path], rubric_yaml_template: str
) -> None:
    pack_dir = pack_factory()
    (pack_dir / "rubric.yaml").write_text(
        rubric_yaml_template.replace("f1_symptom: the code is a mess", "f1_symptom: ''"),
        encoding="utf-8",
    )
    report = lint_task_pack_dir(pack_dir)
    assert "rubric-f1-symptom" in _rules(report)
    assert not report.ok


def test_broken_task_yaml_is_reported(pack_factory: Callable[[str], Path]) -> None:
    pack_dir = pack_factory()
    (pack_dir / "task.yaml").write_text("id: [unclosed\n", encoding="utf-8")
    report = lint_task_pack_dir(pack_dir)
    assert "task-yaml" in _rules(report)
    assert not report.ok


def test_repo_has_ten_real_packs() -> None:
    packs = sorted(p for p in TASKS_DIR.iterdir() if p.is_dir())
    assert len(packs) == 10


@pytest.mark.parametrize(
    "pack_dir",
    sorted(p for p in TASKS_DIR.iterdir() if p.is_dir()),
    ids=lambda p: p.name,
)
def test_real_task_packs_pass_lint(pack_dir: Path) -> None:
    report = lint_task_pack_dir(pack_dir)
    assert report.ok, f"{pack_dir.name}: " + "; ".join(str(issue) for issue in report.errors)
    # warnings (e.g. unpinned-on-host digests) are reported but do not fail
    for issue in report.warnings:
        print(f"{pack_dir.name}: warning: {issue}")


def test_real_task_packs_load() -> None:
    for pack_dir in sorted(p for p in TASKS_DIR.iterdir() if p.is_dir()):
        pack = load_task_pack(pack_dir)
        assert pack.task_id == pack_dir.name
        assert len(pack.rubric.items) > 0
        assert len(pack.defects) > 0
