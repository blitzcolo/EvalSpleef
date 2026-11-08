"""Map verifier check results onto rubric items (defect catalog ``detect.check_ids``).

Each entry of the pack's ``defects.yaml`` names the check ids that prove its rubric
item: ``detect.check_ids`` for ``source: script`` items, ``source: llm_judge`` items
are left to the judge step. This module decides script-kind rubric items only; the
exact matching rules below are the contract, tested against outputs captured from the
real packs under ``tests/observe/fixtures/``.

Matching rules
--------------
Inputs per evaluation: the hard-gate runs (id, passed, captured output) and the
defect catalog parsed from ``defects.yaml``.

A **check id** gets a *failed* signal when any of the following holds:

1. **failing test name** — the id (substring, case-sensitive) appears on a failing
   test line of the captured verifier output: unittest's ``FAIL:`` / ``ERROR:`` lines
   or the packs' own ``FAIL:`` summaries (debug-001 items name unittest classes such
   as ``TestMoney``; the hidden suite prints ``FAIL: test_... (....TestMoney)``);
2. **false JSON boolean** — the id matches a boolean key of a JSON report emitted by
   a gate script, and at least one matching value is ``false``. Keys match exactly,
   or case/underscore-insensitively (``checker.RULE_NOT_SELF_CONSISTENT`` matches the
   report key ``rule_self_consistent``), or as a ``<id>_<suffix>`` prefix
   (``verify_submission.HG2`` and ``HG2`` both match ``HG2_format_ok``). Boolean
   values are collected recursively through the whole report;
3. **violation code** — the id's final segment appears as a ``code`` string anywhere
   in a JSON report (``checker.NOT_PURE_JSON`` matches ``{"code": "NOT_PURE_JSON"}``
   inside ``format_violations``);
4. **named hard gate** — the id is exactly a gate id (``HG1``) or a dotted suffix of
   one (``verify_submission.HG1``), and that gate failed. This is the fallback: it
   applies only when the id has no more specific signal of its own.

A check id gets a *passed* signal when none of the failure rules fired and:

- it appears on a passing unittest line (``ok ...``); or
- its matching JSON boolean keys are all ``true``; or
- its named hard gate passed.

- it is a **violation code** (final segment ``UPPER_SNAKE`` with an underscore, such
  as ``checker.RULE_NOT_SELF_CONSISTENT``) that appears nowhere, and the pack's
  checker ran to completion: a gate run that did not time out emitted a JSON report
  carrying a violation list (a list under ``violations`` or ``*_violations``). Such
  codes exist only on failure, so their absence from a completed check is the pass.

A violation code with no signal at all is a *fail* when a ``required`` artifact of
task.yaml is missing from the export: the checker had nothing to inspect, and the
item's pass criteria presuppose that artifact.

Conflicting signals on one id (a ``true`` boolean but a failed named gate) are
*undecidable*: the id contributes no pass.

Detect blocks come from ``defects.yaml``; a rubric item that has no catalog entry
may carry its own ``detect`` block in ``rubric.yaml`` (rubric schema field).

Item verdict (``kind: script`` rubric items):

- **fail** — any of the item's check ids has a failed signal;
- **pass** — no failed signal and at least one check id has a passed signal;
- **inconclusive** — otherwise (no signal at all). Undecidable items are reported as
  ``inconclusive`` and go to the manual queue; they are never silently scored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import yaml

from evalspleef.core.models import CheckKind, Evidence, EvidenceKind

from ..taskpack.models import TaskPack
from ..trial.types import RubricItemResult, RubricVerdict
from .verifier import GateRun

__all__ = ["CatalogDetect", "check_id_signal", "load_catalog_detects", "map_script_items"]

_FAIL_LINE = re.compile(r"^(FAIL|ERROR):", re.IGNORECASE)
_PASS_LINE = re.compile(r"^ok\b")
_FAIL_PROGRESS = re.compile(r"\s\.\.\.\s(FAIL|ERROR)$", re.IGNORECASE)
_VIOLATION_CODE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")
_PASS_PROGRESS = re.compile(r"\s\.\.\.\sok$", re.IGNORECASE)

CheckSignal = Literal["fail", "pass", "undecidable"]

_SIGNAL_FAIL: CheckSignal = "fail"
_SIGNAL_PASS: CheckSignal = "pass"
_SIGNAL_UNDECIDABLE: CheckSignal = "undecidable"


@dataclass(frozen=True)
class CatalogDetect:
    """The ``detect`` block of one defects.yaml entry."""

    rubric_item_id: str
    source: str
    check_ids: tuple[str, ...]


def _detect_entries(pack: TaskPack, name: str, id_key: str) -> list[tuple[str, object]]:
    """``(item id, detect block)`` pairs from one pack YAML document's ``items`` list."""
    document = yaml.safe_load(pack.file(name).read_text(encoding="utf-8"))
    if not isinstance(document, Mapping):
        return []
    items = document.get("items")
    if not isinstance(items, list):
        return []
    return [
        (str(entry.get(id_key, "")), entry.get("detect"))
        for entry in items
        if isinstance(entry, Mapping)
    ]


def load_catalog_detects(pack: TaskPack) -> dict[str, CatalogDetect]:
    """Per-item detect blocks: ``defects.yaml`` first, then ``rubric.yaml`` for items the
    catalog does not list (the loader keeps only the rubric view, so both are re-read)."""
    detects: dict[str, CatalogDetect] = {}
    entries = [
        *_detect_entries(pack, "defects.yaml", "rubric_item_id"),
        *_detect_entries(pack, "rubric.yaml", "id"),
    ]
    for item_id, detect in entries:
        if not item_id or not isinstance(detect, Mapping) or item_id in detects:
            continue
        raw_ids = detect.get("check_ids")
        check_ids = tuple(str(cid) for cid in raw_ids) if isinstance(raw_ids, list) else ()
        detects[item_id] = CatalogDetect(
            rubric_item_id=item_id,
            source=str(detect.get("source", "script")),
            check_ids=check_ids,
        )
    return detects


def _normalize(key: str) -> str:
    return key.replace("_", "").lower()


def _json_blobs(text: str) -> list[dict[str, object]]:
    decoder = json.JSONDecoder()
    blobs: list[dict[str, object]] = []
    for match in re.finditer(r"\{", text):
        try:
            value, _end = decoder.raw_decode(text[match.start() :])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            blobs.append(value)
    return blobs


def _collect_report_signals(
    blob: Mapping[str, object],
) -> tuple[dict[str, list[bool]], set[str]]:
    """Boolean leaves keyed by normalized name, plus the set of ``code`` strings."""
    booleans: dict[str, list[bool]] = {}
    codes: set[str] = set()

    def visit(value: object, name: str | None) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "code" and isinstance(item, str):
                    codes.add(item)
                visit(item, str(key))
        elif isinstance(value, list):
            for item in value:
                visit(item, name)
        elif isinstance(value, bool) and name is not None:
            booleans.setdefault(_normalize(name), []).append(value)

    visit(blob, None)
    return booleans, codes


def _has_violation_list(value: object) -> bool:
    """Whether a report carries a checker violation list (``violations``/``*_violations``)."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            named = str(key) == "violations" or str(key).endswith("_violations")
            if (named and isinstance(item, list)) or _has_violation_list(item):
                return True
        return False
    if isinstance(value, list):
        return any(_has_violation_list(item) for item in value)
    return False


def _named_gate(check_id: str, gate_ids: Mapping[str, bool]) -> str | None:
    """The hard gate a check id names, if any (``HG1`` or dotted suffix ``x.HG1``)."""
    for gate_id in gate_ids:
        if check_id == gate_id or check_id.endswith(f".{gate_id}"):
            return gate_id
    return None


def _boolean_signal(
    check_id: str, reports: Sequence[tuple[dict[str, list[bool]], set[str]]]
) -> CheckSignal | None:
    """Signal from JSON booleans and violation codes, or ``None`` when no signal.

    Key matching is normalization-insensitive (case/underscores stripped) so
    ``verify_submission.HG2`` and ``HG2`` both match the report key ``HG2_format_ok``.
    """
    norm = _normalize(check_id)
    segment = _normalize(check_id.rsplit(".", 1)[-1])
    saw_boolean = False
    saw_true_only = True
    saw_code = False
    for booleans, codes in reports:
        for key, values in booleans.items():
            if key in (norm, segment) or key.startswith(segment):
                saw_boolean = True
                saw_true_only = saw_true_only and all(values)
        if check_id.rsplit(".", 1)[-1] in codes:
            saw_code = True
    if saw_code:
        return _SIGNAL_FAIL
    if saw_boolean:
        return _SIGNAL_PASS if saw_true_only else _SIGNAL_FAIL
    return None


def _test_name_signal(check_id: str, lines: Sequence[str]) -> CheckSignal | None:
    """Signal from unittest-style result lines mentioning the check id.

    Both unittest line forms count: the summary ``FAIL: test_x (... )`` and the
    verbose progress ``test_x (...) ... FAIL`` (and their ``ok`` counterparts).
    """
    failed = False
    passed = False
    for line in lines:
        if check_id not in line:
            continue
        if _FAIL_LINE.match(line) or _FAIL_PROGRESS.search(line):
            failed = True
        elif _PASS_LINE.match(line) or _PASS_PROGRESS.search(line):
            passed = True
    if failed:
        return _SIGNAL_FAIL
    if passed:
        return _SIGNAL_PASS
    return None


def check_id_signal(
    check_id: str,
    *,
    gate_passed: Mapping[str, bool],
    lines: Sequence[str],
    reports: Sequence[tuple[dict[str, list[bool]], set[str]]],
    checker_completed: bool = False,
    artifact_missing: bool = False,
) -> CheckSignal:
    """The combined signal for one check id; see the module docstring for the rules."""
    named = _named_gate(check_id, gate_passed)
    specific = _test_name_signal(check_id, lines)
    boolean = _boolean_signal(check_id, reports)
    signals = [signal for signal in (specific, boolean) if signal is not None]
    if _SIGNAL_FAIL in signals:
        return _SIGNAL_FAIL
    if named is None:
        if not signals:
            if _VIOLATION_CODE.match(check_id.rsplit(".", 1)[-1]):
                if artifact_missing:
                    return _SIGNAL_FAIL
                if checker_completed:
                    return _SIGNAL_PASS
            return _SIGNAL_UNDECIDABLE
        return (
            _SIGNAL_PASS
            if all(signal == _SIGNAL_PASS for signal in signals)
            else _SIGNAL_UNDECIDABLE
        )
    gate_signal: CheckSignal = _SIGNAL_PASS if gate_passed[named] else _SIGNAL_FAIL
    if not signals:
        return gate_signal
    if all(signal == gate_signal for signal in signals):
        return gate_signal
    return _SIGNAL_UNDECIDABLE


def _verdict(signals: Sequence[CheckSignal]) -> RubricVerdict:
    """Item verdict: any failed id fails the item; otherwise one passing id with no
    failures passes it; ids without any signal do not block the pass. Only when no
    id carries any signal at all is the item undecidable (inconclusive)."""
    if _SIGNAL_FAIL in signals:
        return "fail"
    if any(signal == _SIGNAL_PASS for signal in signals):
        return "pass"
    return "inconclusive"


def map_script_items(
    pack: TaskPack,
    runs: Sequence[GateRun],
    *,
    missing_artifacts: Sequence[str] = (),
) -> tuple[RubricItemResult, ...]:
    """Derive script-kind rubric results from the gate runs.

    ``missing_artifacts`` lists required task.yaml artifacts absent from the export.
    Returns one result per script-kind rubric item; ``llm_judge`` items are the judge
    step's business and are not included here.
    """
    detects = load_catalog_detects(pack)
    gate_passed = {run.gate.gate_id: run.passed for run in runs}
    texts = [run.output for run in runs]
    lines = [line for text in texts for line in text.splitlines()]
    reports = [_collect_report_signals(blob) for text in texts for blob in _json_blobs(text)]
    checker_completed = any(
        _has_violation_list(blob)
        for run in runs
        if not run.timed_out
        for blob in _json_blobs(run.output)
    )

    results: list[RubricItemResult] = []
    for item in pack.rubric.items:
        if item.kind is not CheckKind.SCRIPT:
            continue
        detect = detects.get(item.id)
        if detect is None or not detect.check_ids:
            results.append(RubricItemResult(item.id, "inconclusive"))
            continue
        signals = [
            check_id_signal(
                check_id,
                gate_passed=gate_passed,
                lines=lines,
                reports=reports,
                checker_completed=checker_completed,
                artifact_missing=bool(missing_artifacts),
            )
            for check_id in detect.check_ids
        ]
        verdict = _verdict(signals)
        evidence = tuple(_evidence(run) for run in runs if run.gate.gate_id in detect.check_ids)
        if verdict == "fail" and missing_artifacts:
            evidence += tuple(
                Evidence(
                    kind=EvidenceKind.OTHER,
                    uri=f"artifact://{path}",
                    excerpt="required artifact missing",
                )
                for path in missing_artifacts
            )
        results.append(RubricItemResult(item.id, verdict, evidence=evidence))
    return tuple(results)


def _evidence(run: GateRun) -> Evidence:
    """Gate-backed evidence for an item result."""
    return Evidence(
        kind=EvidenceKind.OTHER,
        uri=f"gate://{run.gate.gate_id}",
        excerpt=f"exit={run.exit_code} stdout={run.stdout[-400:]}",
    )
