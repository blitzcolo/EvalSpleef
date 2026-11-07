"""Tests for the task-pack loader."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from evalspleef.control.taskpack import TaskPackError, load_task_pack
from evalspleef.spi.caps import Arch, Isolation, NetMode, OSFamily


def test_load_pack(pack_factory: Callable[[str], Path]) -> None:
    pack = load_task_pack(pack_factory())
    assert pack.task_id == "test-001"
    assert pack.version == "1.0.0"
    assert pack.track == "logic"

    requirements = pack.requirements
    assert requirements.os is OSFamily.LINUX
    assert requirements.arch == frozenset({Arch.X86_64, Arch.ARM64})
    assert requirements.min_isolation is Isolation.L1
    assert requirements.network is NetMode.OFF
    assert requirements.max_lifetime_s == 7200
    assert requirements.execution_mode == "remote-tools"
    assert requirements.model.tool_calling == "required"
    assert requirements.model.min_context_tokens == 32000

    assert pack.environment["oci_image"] == "llm-eval/test@sha256:abc123"
    assert pack.environment["macos_image"] is None
    assert pack.execution_modes == ("remote-tools",)

    budget = pack.budget
    assert budget.max_rounds == 6
    assert budget.min_required_lifetime_s == 7200
    # A.2 defaults are filled in when the pack omits them
    assert budget.max_feedback_rounds == 3
    assert budget.max_nudges == 2
    assert budget.max_total_tokens == 2_000_000
    assert budget.max_cost_usd == 15.0
    assert budget.escalation.f1_no_progress_rounds == 2

    # rubric and defects land in the core models
    assert [item.id for item in pack.rubric.items] == ["R1", "R2"]
    assert pack.rubric.get("R1").weight == 3  # functional = 3 points
    assert len(pack.defects) == 2
    assert pack.defects.symptom_of("R1") == "the answer looks wrong"
    assert pack.metadata["hard_gates"][0]["id"] == "HG1"


def test_missing_task_yaml(tmp_path: Path) -> None:
    (tmp_path / "test-001").mkdir()
    with pytest.raises(TaskPackError, match=r"task\.yaml"):
        load_task_pack(tmp_path / "test-001")


def test_directory_name_must_match_id(pack_factory: Callable[[str], Path]) -> None:
    pack_dir = pack_factory()
    renamed = pack_dir.parent / "other-001"
    pack_dir.rename(renamed)
    with pytest.raises(TaskPackError, match="does not match directory"):
        load_task_pack(renamed)


def test_missing_runtime_requirements(
    pack_factory: Callable[[str], Path], task_yaml_template: str
) -> None:
    pack_dir = pack_factory(task_yaml_template.replace("  requirements:\n", "  other:\n"))
    with pytest.raises(TaskPackError, match=r"runtime\.requirements"):
        load_task_pack(pack_dir)


def test_unknown_enum_value_names_field(
    pack_factory: Callable[[str], Path], task_yaml_template: str
) -> None:
    pack_dir = pack_factory(task_yaml_template.replace('network: "off"', 'network: "sometimes"'))
    with pytest.raises(TaskPackError, match=r"runtime\.requirements\.network"):
        load_task_pack(pack_dir)


def test_unknown_isolation_names_field(
    pack_factory: Callable[[str], Path], task_yaml_template: str
) -> None:
    pack_dir = pack_factory(task_yaml_template.replace("min_isolation: L1", "min_isolation: L9"))
    with pytest.raises(TaskPackError, match=r"min_isolation"):
        load_task_pack(pack_dir)


def test_unknown_execution_mode(
    pack_factory: Callable[[str], Path], task_yaml_template: str
) -> None:
    pack_dir = pack_factory(
        task_yaml_template.replace(
            "execution_modes: [remote-tools]", "execution_modes: [telepathy]"
        )
    )
    with pytest.raises(TaskPackError, match=r"execution_modes"):
        load_task_pack(pack_dir)


def test_defects_must_reference_rubric_items(
    pack_factory: Callable[[str], Path], defects_yaml_template: str
) -> None:
    pack_dir = pack_factory()
    (pack_dir / "defects.yaml").write_text(
        defects_yaml_template.replace("rubric_item_id: R1", "rubric_item_id: R99"),
        encoding="utf-8",
    )
    with pytest.raises(TaskPackError, match="R99"):
        load_task_pack(pack_dir)


def test_rubric_task_id_mismatch(
    pack_factory: Callable[[str], Path], rubric_yaml_template: str
) -> None:
    pack_dir = pack_factory()
    (pack_dir / "rubric.yaml").write_text(
        rubric_yaml_template.replace("task_id: test-001", "task_id: other-001"),
        encoding="utf-8",
    )
    with pytest.raises(TaskPackError, match=r"task_id"):
        load_task_pack(pack_dir)


def test_invalid_rubric_is_wrapped(pack_factory: Callable[[str], Path]) -> None:
    pack_dir = pack_factory()
    (pack_dir / "rubric.yaml").write_text("items: []\n", encoding="utf-8")
    with pytest.raises(TaskPackError, match=r"rubric\.yaml"):
        load_task_pack(pack_dir)
