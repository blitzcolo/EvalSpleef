"""Conformance report model and writer.

The report is a pure projection of the case results: every case of every selected
target is listed with its status (``pass``, ``fail`` or ``not-applicable``), the
target's ``fake`` flag, and per-target and overall counts. Given a path, both a
JSON and a Markdown document are written side by side.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Status = Literal["pass", "fail", "not-applicable"]


@dataclass(frozen=True)
class CaseResult:
    """Outcome of one case against one target."""

    target: str
    case_id: str
    title: str
    status: Status
    detail: str
    """Failure message, or the not-applicability reason; empty on pass."""

    fake: bool
    """Whether the target runs against a fake of an external system."""


@dataclass(frozen=True)
class TargetSummary:
    """Per-target rollup plus the identity rows of the report."""

    name: str
    kind: str
    provider: str
    fake: bool
    notes: str
    passed: int
    failed: int
    not_applicable: int

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.not_applicable


@dataclass(frozen=True)
class ConformanceReport:
    """All results of one conformance run; renderable as JSON and Markdown."""

    targets: tuple[TargetSummary, ...]
    results: tuple[CaseResult, ...] = ()

    def counts(self) -> dict[str, int]:
        """Aggregate status counts across every target."""
        return {
            "pass": sum(target.passed for target in self.targets),
            "fail": sum(target.failed for target in self.targets),
            "not-applicable": sum(target.not_applicable for target in self.targets),
        }

    def to_dict(self) -> dict[str, object]:
        """Machine-readable form of the report."""
        return {
            "targets": [
                {
                    "name": target.name,
                    "kind": target.kind,
                    "provider": target.provider,
                    "fake": target.fake,
                    "notes": target.notes,
                    "passed": target.passed,
                    "failed": target.failed,
                    "not_applicable": target.not_applicable,
                    "total": target.total,
                }
                for target in self.targets
            ],
            "results": [
                {
                    "target": result.target,
                    "case_id": result.case_id,
                    "title": result.title,
                    "status": result.status,
                    "detail": result.detail,
                    "fake": result.fake,
                }
                for result in self.results
            ],
            "counts": self.counts(),
        }

    def to_json(self) -> str:
        """The report as pretty-printed JSON."""
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n"

    def to_markdown(self) -> str:
        """The report as a Markdown document."""
        lines = ["# Conformance report", ""]
        total = self.counts()
        lines.append(
            f"Totals: {total['pass']} passed, {total['fail']} failed, "
            f"{total['not-applicable']} not-applicable."
        )
        lines.append("")
        lines.append("## Targets")
        lines.append("")
        lines.append(
            "| Target | Kind | Provider | Fake | Passed | Failed | Not applicable | Notes |"
        )
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for target in self.targets:
            lines.append(
                f"| {target.name} | {target.kind} | {target.provider} | {target.fake} "
                f"| {target.passed} | {target.failed} | {target.not_applicable} | {target.notes} |"
            )
        lines.append("")
        lines.append("## Cases")
        lines.append("")
        lines.append("| Target | Case | Title | Status | Fake | Detail |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for result in self.results:
            lines.append(
                f"| {result.target} | {result.case_id} | {result.title} | {result.status} "
                f"| {result.fake} | {result.detail} |"
            )
        lines.append("")
        return "\n".join(lines)

    def write(self, path: str | Path) -> tuple[Path, Path]:
        """Write the JSON and Markdown reports next to ``path``.

        A recognised suffix (``.json`` / ``.md`` / ``.markdown``) is replaced by
        the other format's suffix; anything else gets both suffixes appended.
        Returns the ``(json_path, markdown_path)`` actually written.
        """
        requested = Path(path)
        stem = requested
        if requested.suffix in {".json", ".md", ".markdown"}:
            stem = requested.with_suffix("")
        json_path = stem.with_suffix(".json")
        markdown_path = stem.with_suffix(".md")
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(self.to_json(), encoding="utf-8")
        markdown_path.write_text(self.to_markdown(), encoding="utf-8")
        return json_path, markdown_path
