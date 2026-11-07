"""Task-pack linter: PRD FR-TSK-03 rules plus the appendix E.1 pack contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .loader import REFERENCE_GLOBS, REQUIRED_FILES, _read_yaml
from .models import Budget

__all__ = ["LintIssue", "LintReport", "lint_task_pack_dir"]

#: Marker allowed in image references until the digest is pinned on a build
#: host; packs carrying it pass with a warning instead of an error.
PIN_ON_HOST_MARKER = "TODO(pin-on-host)"

ENVIRONMENT_IMAGES = ("oci_image", "macos_image", "windows_image")

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class LintIssue:
    """One lint finding; ``rule`` is a stable machine-readable identifier."""

    severity: Severity
    rule: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity} [{self.rule}] {self.path}: {self.message}"


@dataclass
class LintReport:
    """The full lint result for one pack; ``ok`` means no errors."""

    task_id: str
    issues: list[LintIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[LintIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[LintIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_errors(self) -> None:
        if self.errors:
            rendered = "\n".join(str(issue) for issue in self.errors)
            raise ValueError(f"task pack {self.task_id!r} failed lint:\n{rendered}")


def _issue(
    issues: list[LintIssue],
    severity: Severity,
    rule: str,
    path: str,
    message: str,
) -> None:
    issues.append(LintIssue(severity=severity, rule=rule, path=path, message=message))


def _lint_required_files(root: Path, issues: list[LintIssue]) -> None:
    for relative in REQUIRED_FILES:
        if not (root / relative).is_file():
            _issue(issues, "error", "required-files", relative, "required file is missing (E.1)")
    for pattern in REFERENCE_GLOBS:
        if not any((root / "answers").glob(Path(pattern).name)):
            _issue(
                issues,
                "error",
                "required-files",
                pattern,
                "reference solution is missing and must differ in approach from the other (E.1)",
            )


def _lint_images(
    task: dict[str, Any],
    issues: list[LintIssue],
) -> None:
    runtime = task.get("runtime")
    if not isinstance(runtime, dict):
        return
    environment = runtime.get("environment")
    if not isinstance(environment, dict):
        return
    for key in ENVIRONMENT_IMAGES:
        image = environment.get(key)
        if image is None:
            continue
        ref = str(image)
        if "@sha256:" in ref:
            continue
        if PIN_ON_HOST_MARKER in ref:
            _issue(
                issues,
                "warning",
                "image-digest-pending",
                f"runtime.environment.{key}",
                "image digest is not pinned yet; pin it on the build host before running",
            )
            continue
        _issue(
            issues,
            "error",
            "image-digest",
            f"runtime.environment.{key}",
            "image reference must pin a sha256 digest (FR-TSK-03)",
        )


def _lint_lifetime(task: dict[str, Any], issues: list[LintIssue]) -> None:
    runtime = task.get("runtime")
    requirements = runtime.get("requirements") if isinstance(runtime, dict) else None
    if not isinstance(requirements, dict):
        return  # the dedicated rule below reports the missing block
    lifetime = requirements.get("max_lifetime_s")
    budget_block = task.get("budget")
    budget = Budget.from_dict(budget_block if isinstance(budget_block, dict) else {})
    if lifetime is None:
        return
    if int(lifetime) < budget.min_required_lifetime_s:
        _issue(
            issues,
            "error",
            "lifetime",
            "runtime.requirements.max_lifetime_s",
            f"max_lifetime_s ({lifetime}) must be >= budget.max_wall_clock_s + "
            f"max_rounds * max_observe_s = {budget.min_required_lifetime_s} (FR-TSK-03)",
        )


def _lint_requirements_present(task: dict[str, Any], issues: list[LintIssue]) -> None:
    runtime = task.get("runtime")
    if not isinstance(runtime, dict):
        _issue(issues, "error", "runtime-requirements", "runtime", "'runtime' block is required")
        return
    if not isinstance(runtime.get("requirements"), dict):
        _issue(
            issues,
            "error",
            "runtime-requirements",
            "runtime.requirements",
            "'runtime.requirements' is required (FR-TSK-01/FR-TSK-03)",
        )


def _lint_gateway_egress(task: dict[str, Any], issues: list[LintIssue]) -> None:
    runtime = task.get("runtime")
    if not isinstance(runtime, dict):
        return
    modes = runtime.get("execution_modes", ["remote-tools"])
    if not isinstance(modes, list) or "in-sandbox" not in modes:
        return
    requirements = runtime.get("requirements")
    allow_hosts: list[object] = []
    network_mode = "off"
    if isinstance(requirements, dict):
        raw = requirements.get("allow_hosts", [])
        allow_hosts = list(raw) if isinstance(raw, list) else []
        network_mode = str(requirements.get("network", "off"))
    top_network = task.get("network")
    top_allowlist = top_network.get("allowlist", []) if isinstance(top_network, dict) else []
    declared = bool(allow_hosts) or bool(top_allowlist) or network_mode != "off"
    if not declared:
        _issue(
            issues,
            "error",
            "gateway-egress",
            "runtime",
            "tasks declaring 'in-sandbox' must declare a gateway egress: set "
            "runtime.requirements.network to a non-off mode or list the gateway "
            "host in allow_hosts (FR-TSK-03)",
        )


def _lint_hidden_tests(root: Path, task: dict[str, Any], issues: list[LintIssue]) -> None:
    """Hidden tests must never sit inside the agent-visible starter (I10, E.1)."""
    workspace = task.get("workspace")
    starter = "workspace/"
    if isinstance(workspace, dict) and workspace.get("starter"):
        starter = str(workspace["starter"])
    starter_root = (root / starter).resolve()
    if not starter_root.is_dir():
        return  # the loader reports a missing starter when it is required
    for path in starter_root.rglob("*"):
        if path.name == "hidden-tests" or "hidden-tests" in path.parts:
            relative = path.relative_to(root)
            _issue(
                issues,
                "error",
                "hidden-tests-isolation",
                str(relative),
                "hidden tests must not be reachable from the agent starter (I10, E.1)",
            )


def _lint_rubric_and_defects(root: Path, issues: list[LintIssue]) -> None:
    rubric_path = root / "rubric.yaml"
    if not rubric_path.is_file():
        return  # required-files already reports this
    rubric = _read_yaml(rubric_path)
    items = rubric.get("items")
    rubric_ids: set[str] = set()
    if isinstance(items, list):
        for entry in items:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", ""))
            rubric_ids.add(item_id)
            if not str(entry.get("f1_symptom", "")).strip():
                _issue(
                    issues,
                    "error",
                    "rubric-f1-symptom",
                    f"rubric.yaml items[{item_id}]",
                    "every rubric item must carry a non-empty f1_symptom (E.1)",
                )
    defects_path = root / "defects.yaml"
    if not defects_path.is_file():
        return
    defects = _read_yaml(defects_path)
    defect_items = defects.get("items")
    if not isinstance(defect_items, list):
        return
    for index, entry in enumerate(defect_items):
        if not isinstance(entry, dict):
            continue
        item_id = str(entry.get("rubric_item_id", ""))
        if rubric_ids and item_id not in rubric_ids:
            _issue(
                issues,
                "error",
                "defects-link",
                f"defects.yaml items[{index}]",
                f"defect catalogue entry references unknown rubric item {item_id!r}",
            )


def lint_task_pack_dir(root: str | Path) -> LintReport:
    """Lint one pack directory without loading it into the core models.

    Missing or malformed ``task.yaml`` is itself a finding; the remaining
    rules degrade gracefully so one broken file does not hide other problems.
    """
    pack_root = Path(root)
    task_id = pack_root.name
    issues: list[LintIssue] = []

    _lint_required_files(pack_root, issues)

    task: dict[str, Any] = {}
    task_file = pack_root / "task.yaml"
    if task_file.is_file():
        try:
            document = yaml.safe_load(task_file.read_text(encoding="utf-8"))
            if isinstance(document, dict):
                task = document
            else:
                _issue(issues, "error", "task-yaml", "task.yaml", "top level must be a mapping")
        except yaml.YAMLError as exc:
            _issue(issues, "error", "task-yaml", "task.yaml", f"invalid YAML: {exc}")

    _lint_requirements_present(task, issues)
    _lint_images(task, issues)
    _lint_lifetime(task, issues)
    _lint_gateway_egress(task, issues)
    _lint_hidden_tests(pack_root, task, issues)
    _lint_rubric_and_defects(pack_root, issues)

    return LintReport(task_id=task_id, issues=issues)
