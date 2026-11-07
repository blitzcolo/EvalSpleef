"""Task-pack loader (PRD appendix E.1)."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from evalspleef.core.defects import DefectCatalog
from evalspleef.core.models import RubricSet
from evalspleef.spi.caps import ExecutionMode

from .errors import TaskPackError
from .models import Budget, TaskPack
from .requirements import requirements_from_dict

__all__ = ["REQUIRED_FILES", "load_task_pack"]

#: Files every pack must contain (PRD appendix E.1). Reference solutions are
#: matched by glob (``answers/reference-a.*``) because the extension varies.
REQUIRED_FILES: tuple[str, ...] = (
    "task.yaml",
    "instruction.md",
    "persona.md",
    "rubric.yaml",
    "defects.yaml",
    "hidden-tests/run.sh",
    "canary.md",
    "notes.md",
)

REFERENCE_GLOBS: tuple[str, ...] = ("answers/reference-a.*", "answers/reference-b.*")

_EXECUTION_MODES: tuple[str, ...] = ("remote-tools", "in-sandbox")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TaskPackError(f"task pack: cannot read {path}: {exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise TaskPackError(f"task pack: invalid YAML in {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise TaskPackError(f"task pack: {path} must contain a YAML mapping")
    return document


def _load_rubric(root: Path) -> RubricSet:
    document = _read_yaml(root / "rubric.yaml")
    try:
        return RubricSet.from_dict(document)
    except (KeyError, ValueError, TypeError) as exc:
        raise TaskPackError(f"task pack: rubric.yaml: {exc}") from exc


def _load_defects(root: Path, rubric: RubricSet) -> DefectCatalog:
    """Load the defect catalogue into the core DefectCatalog model.

    Pack entries are keyed by ``rubric_item_id``; they map onto core rubric
    items so the F1/F2/F3 symptom templates live in one model. Every entry
    must reference an existing rubric item (E.1: the catalogue aligns with
    the rubric).
    """
    document = _read_yaml(root / "defects.yaml")
    items = document.get("items")
    if not isinstance(items, list) or not items:
        raise TaskPackError("task pack: defects.yaml must contain a non-empty 'items' list")
    rubric_ids = {item.id for item in rubric.items}
    converted: list[dict[str, Any]] = []
    for entry in items:
        if not isinstance(entry, Mapping):
            raise TaskPackError(f"task pack: defects.yaml items must be mappings, got {entry!r}")
        item_id = str(entry.get("rubric_item_id", ""))
        if item_id not in rubric_ids:
            raise TaskPackError(
                f"task pack: defects.yaml entry references unknown rubric item {item_id!r}"
            )
        converted.append(
            {
                "id": item_id,
                "severity": str(entry.get("severity", "minor")),
                "description": str(entry.get("title", "")),
                "f1_symptom": str(entry.get("f1_symptom", "")),
            }
        )
    try:
        return DefectCatalog.from_dicts(converted)
    except (KeyError, ValueError, TypeError) as exc:
        raise TaskPackError(f"task pack: defects.yaml: {exc}") from exc


def _execution_modes(runtime: Mapping[str, Any], context: str) -> tuple[ExecutionMode, ...]:
    raw = runtime.get("execution_modes", ["remote-tools"])
    if not isinstance(raw, list | tuple) or not raw:
        raise TaskPackError(f"task pack: {context}.execution_modes must be a non-empty list")
    modes: list[ExecutionMode] = []
    for item in raw:
        if item not in _EXECUTION_MODES:
            raise TaskPackError(
                f"task pack: {context}.execution_modes: unknown mode {item!r}; "
                f"expected one of {_EXECUTION_MODES}"
            )
        modes.append(item)
    return tuple(modes)


def load_task_pack(root: str | Path) -> TaskPack:
    """Load ``tasks/<task-id>/`` into a :class:`TaskPack`.

    Raises :class:`TaskPackError` naming the offending file and field; the
    rubric and defect catalogues come out as the core models.
    """
    pack_root = Path(root)
    if not pack_root.is_dir():
        raise TaskPackError(f"task pack: {pack_root} is not a directory")
    task_file = pack_root / "task.yaml"
    if not task_file.is_file():
        raise TaskPackError(f"task pack: {pack_root}: required file 'task.yaml' is missing")

    metadata = _read_yaml(task_file)
    task_id = str(metadata.get("id", ""))
    if not task_id:
        raise TaskPackError(f"task pack: {task_file}: 'id' is required")
    if task_id != pack_root.name:
        raise TaskPackError(
            f"task pack: task.yaml id {task_id!r} does not match directory {pack_root.name!r}"
        )

    runtime = metadata.get("runtime")
    if not isinstance(runtime, Mapping):
        raise TaskPackError(f"task pack: {task_file}: 'runtime' block is required (FR-TSK-01)")
    requirements_block = runtime.get("requirements")
    if not isinstance(requirements_block, Mapping):
        raise TaskPackError(
            f"task pack: {task_file}: 'runtime.requirements' is required (FR-TSK-01)"
        )
    modes = _execution_modes(runtime, "runtime")
    model_requirements = runtime.get("model_requirements", {})
    if not isinstance(model_requirements, Mapping):
        raise TaskPackError("task pack: runtime.model_requirements must be a mapping")
    requirements = requirements_from_dict(
        requirements_block,
        execution_mode=modes[0],
        model_requirements=model_requirements,
        context="runtime.requirements",
    )
    environment_raw = runtime.get("environment", {})
    if not isinstance(environment_raw, Mapping):
        raise TaskPackError("task pack: runtime.environment must be a mapping")
    environment: dict[str, str | None] = {
        str(key): (None if value is None else str(value)) for key, value in environment_raw.items()
    }

    budget_block = metadata.get("budget", {})
    if not isinstance(budget_block, Mapping):
        raise TaskPackError("task pack: budget must be a mapping")
    budget = Budget.from_dict(budget_block)

    rubric = _load_rubric(pack_root)
    if rubric.task_id != task_id:
        raise TaskPackError(
            f"task pack: rubric.yaml task_id {rubric.task_id!r} does not match {task_id!r}"
        )
    defects = _load_defects(pack_root, rubric)

    return TaskPack(
        task_id=task_id,
        version=str(metadata.get("version", "")),
        track=str(metadata.get("track", "")),
        root=pack_root,
        budget=budget,
        requirements=requirements,
        environment=environment,
        execution_modes=modes,
        rubric=rubric,
        defects=defects,
        metadata=metadata,
    )
