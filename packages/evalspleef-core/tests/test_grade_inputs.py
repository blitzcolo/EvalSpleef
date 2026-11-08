"""grade_inputs.py: the recorded grading inputs round-trip through JSON and regrade identically."""

from __future__ import annotations

import json
import unittest

from evalspleef.core.feedback import FeedbackRound
from evalspleef.core.grade_inputs import GradingInputs
from evalspleef.core.grading import HardGate
from evalspleef.core.models import (
    Budget,
    BudgetUsage,
    Defect,
    DefectProgress,
    DefectStatus,
    Evidence,
    EvidenceKind,
    FeedbackLevel,
    Severity,
)

TRIAL_ID = "web-001.demo-model.terminus.e1.t01"


def make_inputs() -> GradingInputs:
    return GradingInputs(
        trial_id=TRIAL_ID,
        defects=(
            Defect(
                defect_id="R1#1",
                rubric_item_id="R1",
                severity=Severity.FUNCTIONAL,
                status=DefectStatus.FIXED,
                first_round=1,
                last_round=2,
                rounds_failed=(1,),
                fixed_round=2,
                feedback_rounds=1,
                max_level_during_open=FeedbackLevel.F1,
                progress=DefectProgress.FIXED,
                evidence=(Evidence(kind=EvidenceKind.SCREENSHOT, uri="s3://x/r1.png"),),
                judge={"verdict": "pass"},
                note="fixed after F1",
            ),
            Defect(
                defect_id="R3#1",
                rubric_item_id="R3",
                severity=Severity.MINOR,
                status=DefectStatus.OPEN,
                first_round=1,
                last_round=2,
                rounds_failed=(1, 2),
            ),
        ),
        hard_gates=(HardGate(check_id="HG1", passed=True, evidence_uri="s3://x/log"),),
        feedback_rounds=(
            FeedbackRound(
                round_no=1, level=FeedbackLevel.F1, defect_ids=("R1#1",), made_progress=True
            ),
        ),
        budget=Budget(),
        usage=BudgetUsage(rounds=2, feedback_rounds=1, total_tokens=1234, wall_clock_s=12.3456789),
        judge_consistency_value=0.9,
        judge_samples=5,
        rubric_version="1.3.0",
        task_version="2.0.0",
        pricing_table_version="2026-11",
    )


class GradingInputsTest(unittest.TestCase):
    def test_round_trip_through_json_is_exact(self) -> None:
        inputs = make_inputs()
        parsed = GradingInputs.from_dict(json.loads(json.dumps(inputs.to_dict())))
        self.assertEqual(parsed, inputs)

    def test_regrading_the_recorded_inputs_gives_the_same_grade(self) -> None:
        inputs = make_inputs()
        first = inputs.grade(decided_at="2026-11-08T00:00:00Z")
        again = GradingInputs.from_dict(json.loads(json.dumps(inputs.to_dict()))).grade(
            decided_at="2026-11-08T00:00:00Z"
        )
        assert first.grade is not None and again.grade is not None
        self.assertEqual(again.grade.to_dict(), first.grade.to_dict())
        self.assertEqual(again.rule_id, first.rule_id)


if __name__ == "__main__":
    unittest.main()
