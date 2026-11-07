"""Task-pack loading and linting.

A task pack is a ``tasks/<task-id>/`` directory per PRD appendix E.1. The
loader reads ``task.yaml``, ``rubric.yaml`` and ``defects.yaml`` into the core
models; the linter enforces the pack contract (E.1) and the new rules of
FR-TSK-03.
"""

from .errors import TaskPackError
from .linter import LintIssue, LintReport, lint_task_pack_dir
from .loader import REQUIRED_FILES, load_task_pack
from .models import Budget, TaskPack
from .requirements import requirements_from_dict

__all__ = [
    "REQUIRED_FILES",
    "Budget",
    "LintIssue",
    "LintReport",
    "TaskPack",
    "TaskPackError",
    "lint_task_pack_dir",
    "load_task_pack",
    "requirements_from_dict",
]
