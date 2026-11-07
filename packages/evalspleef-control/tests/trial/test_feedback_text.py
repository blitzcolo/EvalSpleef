"""F0-F3 message text: built from the pack's defect catalogue and persona only."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from evalspleef.control.taskpack import TaskPack, load_task_pack
from evalspleef.control.trial import PackFeedbackWriter
from evalspleef.control.trial.feedback_text import NUDGE_MAX_CHARS
from evalspleef.core.feedback import (
    ALLOWED_FIELDS,
    F0Policy,
    FeedbackDecision,
    LeakCategory,
    LeakViolation,
    RegexLeakChecker,
)
from evalspleef.core.models import FeedbackLevel

REPO = Path(__file__).resolve().parents[4]
ITEM_ID = re.compile(r"\bR\d{1,3}\b")


def decision(
    level: FeedbackLevel, item_id: str, allowed: frozenset[str] | None = None
) -> FeedbackDecision:
    return FeedbackDecision(
        level=level,
        defect_id=f"{item_id}#1",
        allowed_fields=ALLOWED_FIELDS[level] if allowed is None else allowed,
    )


def catalogue(pack: TaskPack) -> dict[str, dict[str, Any]]:
    document = yaml.safe_load((pack.root / "defects.yaml").read_text(encoding="utf-8"))
    return {entry["rubric_item_id"]: entry for entry in document["items"]}


@pytest.fixture
def debug_pack() -> TaskPack:
    return load_task_pack(REPO / "tasks" / "debug-001")


@pytest.fixture
def visible(pack: TaskPack, blocking: str, tmp_path: Path) -> tuple[TaskPack, dict[str, Any]]:
    """A copy of the backend pack whose items are visible and whose first one has F1-F3 material."""
    root = tmp_path / "pack" / pack.task_id
    shutil.copytree(pack.root, root)
    rubric = yaml.safe_load((root / "rubric.yaml").read_text(encoding="utf-8"))
    for item in rubric["items"]:
        item.pop("hidden", None)
    (root / "rubric.yaml").write_text(yaml.safe_dump(rubric, allow_unicode=True), encoding="utf-8")
    defects = yaml.safe_load((root / "defects.yaml").read_text(encoding="utf-8"))
    entry = next(e for e in defects["items"] if e["rubric_item_id"] == blocking)
    entry["f3_visual"] = {"description": "订单列表里同一个订单出现了两行。"}
    entry["f3_assist"] = {"action": "other", "instruction": "我可以帮你重启一下依赖服务。"}
    (root / "defects.yaml").write_text(
        yaml.safe_dump(defects, allow_unicode=True), encoding="utf-8"
    )
    return load_task_pack(root), entry


def test_f1_is_the_symptom_and_nothing_else(pack: TaskPack, blocking: str) -> None:
    writer = PackFeedbackWriter.load(pack)
    entry = catalogue(pack)[blocking]
    text = writer.compose(decision(FeedbackLevel.F1, blocking))
    assert text == entry["f1_symptom"].strip()
    assert not ITEM_ID.search(text)


def test_f2_and_f3_add_exactly_their_own_fields(
    visible: tuple[TaskPack, dict[str, Any]],
) -> None:
    pack, entry = visible
    writer = PackFeedbackWriter.load(pack)
    item_id = entry["rubric_item_id"]
    symptom = entry["f1_symptom"].strip()
    evidence = entry["f2_evidence"]["text"].strip()
    visual = entry["f3_visual"]["description"]
    assist = entry["f3_assist"]["instruction"]

    f1 = writer.compose(decision(FeedbackLevel.F1, item_id))
    f2 = writer.compose(decision(FeedbackLevel.F2, item_id))
    f3 = writer.compose(decision(FeedbackLevel.F3, item_id))

    assert symptom in f1
    assert evidence not in f1 and visual not in f1 and assist not in f1
    assert symptom in f2 and evidence in f2
    assert visual not in f2 and assist not in f2
    assert symptom in f3 and evidence in f3 and visual in f3 and assist in f3
    for text in (f1, f2, f3):
        assert not ITEM_ID.search(text)
        assert entry["f2_evidence"]["source_command"] not in text
    # The messages stay clean for the default leak checker at their own level.
    checker = RegexLeakChecker()
    assert not checker.check(f1, FeedbackLevel.F1)
    assert not checker.check(f3, FeedbackLevel.F3)


def test_shipped_hidden_items_never_show_more_than_their_symptom(debug_pack: TaskPack) -> None:
    writer = PackFeedbackWriter.load(debug_pack)
    entries = catalogue(debug_pack)
    hidden = [item.id for item in debug_pack.rubric.items if item.hidden and item.id in entries]
    assert any(entries[item_id].get("f3_assist") for item_id in hidden)
    for item_id in hidden:
        symptom = entries[item_id]["f1_symptom"].strip()
        for level in (FeedbackLevel.F1, FeedbackLevel.F2, FeedbackLevel.F3):
            assert writer.compose(decision(level, item_id)) == symptom


def test_hidden_items_show_their_symptom_at_every_level(pack: TaskPack, blocking: str) -> None:
    entry = catalogue(pack)[blocking]
    assert entry.get("f2_evidence")  # the material exists; it must stay unused
    writer = PackFeedbackWriter.load(pack)
    symptom = entry["f1_symptom"].strip()
    assert {item.id for item in pack.rubric.items if item.hidden} >= {blocking}
    for level in (FeedbackLevel.F1, FeedbackLevel.F2, FeedbackLevel.F3):
        assert writer.compose(decision(level, blocking)) == symptom


def test_the_decisions_allowed_fields_limit_the_text(
    visible: tuple[TaskPack, dict[str, Any]],
) -> None:
    pack, entry = visible
    writer = PackFeedbackWriter.load(pack)
    only_symptom = decision(FeedbackLevel.F3, entry["rubric_item_id"], frozenset({"f1_symptom"}))
    assert writer.compose(only_symptom) == entry["f1_symptom"].strip()


def test_no_text_without_a_defect_or_material(pack: TaskPack, blocking: str) -> None:
    writer = PackFeedbackWriter.load(pack)
    assert writer.compose(FeedbackDecision(FeedbackLevel.F0, None, frozenset())) == ""
    assert writer.compose(decision(FeedbackLevel.F1, "R999")) == ""
    # A minor item has no catalogue entry and never reaches the user.
    minor = next(i.id for i in pack.rubric.items if i.id not in catalogue(pack))
    assert writer.compose(decision(FeedbackLevel.F1, minor)) == ""


def test_nudges_carry_no_information(pack: TaskPack) -> None:
    writer = PackFeedbackWriter.load(pack)
    checker = RegexLeakChecker()
    nudges = {writer.nudge(index) for index in range(6)}
    assert nudges
    for nudge in nudges:
        assert 0 < len(nudge) <= NUDGE_MAX_CHARS
        assert not checker.check(nudge, FeedbackLevel.F0)
    assert writer.nudge(0) == writer.nudge(len(nudges))


def test_nudge_falls_back_to_the_generic_line_when_the_persona_has_none() -> None:
    writer = PackFeedbackWriter({}, [], {})
    assert writer.nudge(3) == F0Policy.GENERIC_CONTINUE


def test_faq_keys_match_questions_despite_punctuation() -> None:
    writer = PackFeedbackWriter({}, [], {"能装 FastAPI 吗？": "沙箱没网。"})
    assert writer.answer("请问能装 FastAPI 吗") == "沙箱没网。"
    assert writer.answer("想问点别的") == F0Policy.GENERIC_ANSWER


def test_rewrite_drops_the_flagged_sentences_only() -> None:
    writer = PackFeedbackWriter({}, [], {})
    violations = [LeakViolation(LeakCategory.FILE_NAME, "leak", matched="api.py")]
    assert (
        writer.rewrite("先看现象。问题在 api.py 里。再试一次。", violations)
        == "先看现象。再试一次。"
    )
    assert writer.rewrite("只有 api.py 这一句。", violations) == ""
