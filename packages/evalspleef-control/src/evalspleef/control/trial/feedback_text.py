"""The words of the simulated user: F0-F3 messages from the pack's own materials.

Only ``defects.yaml`` (the pre-written symptom, evidence and assist texts) and ``persona.md``
(F0 lines and FAQ) are read. Rubric ids, hidden-test content, severities and file paths never
reach a message. Which fields a level may show is decided by ``core.feedback`` (PRD appendix
C.2, section 7.3): ``filter_allowed_fields`` is the single implementation of that matrix.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from evalspleef.core.feedback import (
    F0Policy,
    FeedbackDecision,
    LeakViolation,
    RegexLeakChecker,
    filter_allowed_fields,
)
from evalspleef.core.models import FeedbackLevel, parse_defect_id

from ..taskpack.models import TaskPack

__all__ = ["DefectMaterial", "PackFeedbackWriter"]

NUDGE_MAX_CHARS = 60
"""A nudge may not exceed the ``maxLength`` of ``feedback.schema.json``."""

_F2_LEAD = "我这边看到的原文是："
_F3_VISUAL_LEAD = "看到的差异是："
_QUOTES = "\"'“”「」‘’"
_HEADING = re.compile(r"^#{2,3}\s*(.+?)\s*$")
_SENTENCE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;\n]*")
_FAQ_PUNCTUATION = re.compile(r"[\s，。！？,.!?：:；;]+")


@dataclass(frozen=True)
class DefectMaterial:
    """The pre-written texts of one defect; empty string means the pack provides none."""

    item_id: str
    f1_symptom: str
    f2_evidence: str
    f3_visual: str
    f3_assist: str
    hidden: bool


def _text(value: Any, *keys: str) -> str:
    """A text that is either a plain string or the first present key of a mapping."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _unquote(text: str) -> str:
    return text.strip().strip(_QUOTES).strip()


def _load_materials(pack: TaskPack) -> dict[str, DefectMaterial]:
    document = yaml.safe_load((pack.root / "defects.yaml").read_text(encoding="utf-8"))
    items = document.get("items", ()) if isinstance(document, Mapping) else ()
    rubric_hidden = {item.id for item in pack.rubric.items if item.hidden}
    materials: dict[str, DefectMaterial] = {}
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        item_id = str(entry.get("rubric_item_id", ""))
        if not item_id:
            continue
        materials[item_id] = DefectMaterial(
            item_id=item_id,
            f1_symptom=_text(entry.get("f1_symptom")),
            f2_evidence=_text(entry.get("f2_evidence"), "text"),
            f3_visual=_text(entry.get("f3_visual"), "description"),
            f3_assist=_text(entry.get("f3_assist"), "instruction"),
            hidden=bool(entry.get("hidden")) or item_id in rubric_hidden,
        )
    return materials


def _parse_persona(text: str) -> tuple[list[str], dict[str, str]]:
    """The F0 lines and the FAQ table of ``persona.md``."""
    f0_lines: list[str] = []
    faq: dict[str, str] = {}
    section = ""
    header_skipped = False
    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            title = heading.group(1).upper()
            section = "f0" if title.startswith("F0") else "faq" if title.startswith("FAQ") else ""
            header_skipped = False
            continue
        stripped = line.strip()
        if section == "f0" and stripped.startswith(("- ", "* ")):
            f0_lines.append(_unquote(stripped[2:]))
        elif section == "faq" and stripped.startswith("|"):
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            if len(cells) < 2 or all(re.fullmatch(r"[\s:\-]*", cell) for cell in cells):
                continue
            if not header_skipped:
                header_skipped = True
                continue
            faq[_unquote(cells[0])] = _unquote(cells[1])
    return f0_lines, faq


class PackFeedbackWriter:
    """Deterministic ``FeedbackWriter`` over the pack's defect catalogue and persona."""

    def __init__(
        self,
        materials: Mapping[str, DefectMaterial],
        f0_lines: Sequence[str],
        faq: Mapping[str, str],
    ) -> None:
        self._materials = dict(materials)
        checker = RegexLeakChecker()
        usable = [
            line
            for line in f0_lines
            if 0 < len(line) <= NUDGE_MAX_CHARS and not checker.check(line, FeedbackLevel.F0)
        ]
        self._nudges = tuple(usable) or (F0Policy.GENERIC_CONTINUE,)
        # F0Policy strips punctuation from the question but not from the keys: do it for both.
        plain = {_FAQ_PUNCTUATION.sub("", key): answer for key, answer in faq.items()}
        self._policy = F0Policy(faq={key: answer for key, answer in plain.items() if key})

    @classmethod
    def load(cls, pack: TaskPack) -> PackFeedbackWriter:
        persona = (pack.root / "persona.md").read_text(encoding="utf-8")
        f0_lines, faq = _parse_persona(persona)
        return cls(_load_materials(pack), f0_lines, faq)

    def compose(self, decision: FeedbackDecision) -> str:
        if decision.defect_id is None or decision.level is FeedbackLevel.F0:
            return ""
        material = self._materials.get(parse_defect_id(decision.defect_id)[0])
        if material is None:
            return ""
        offered = {
            "f1_symptom": material.f1_symptom,
            "f2_evidence": material.f2_evidence,
            "f3_visual": material.f3_visual,
            "f3_assist": material.f3_assist,
        }
        wanted = {
            name: text for name, text in offered.items() if text and name in decision.allowed_fields
        }
        kept, _dropped = filter_allowed_fields(wanted, decision.level, hidden=material.hidden)
        if "f1_symptom" not in kept:
            return ""
        parts = [kept["f1_symptom"]]
        if "f2_evidence" in kept:
            parts.append(f"{_F2_LEAD}{kept['f2_evidence']}")
        if "f3_visual" in kept:
            parts.append(f"{_F3_VISUAL_LEAD}{kept['f3_visual']}")
        if "f3_assist" in kept:
            parts.append(kept["f3_assist"])
        return "\n".join(parts)

    def rewrite(self, text: str, violations: Sequence[LeakViolation]) -> str:
        flagged = [violation.matched for violation in violations if violation.matched]
        kept = [s for s in _SENTENCE.findall(text) if not any(item in s for item in flagged)]
        return "".join(kept).strip()

    def nudge(self, index: int) -> str:
        return self._nudges[index % len(self._nudges)]

    def answer(self, question: str) -> str:
        return self._policy.respond_to_question(question)
