from __future__ import annotations

import unittest

from evalspleef_harbor_bridge.events import (
    HARBOR_CAUSE_TO_ERROR_CLASS,
    HarborAdapter,
    error_from_harbor,
    event_to_machine_event,
)

from evalspleef.core.models import ErrorClass, TrialState

TRIAL_A = "web-001.claude-sonnet-5.claude-code.e1.t01"


class AdapterTest(unittest.TestCase):
    def test_harbor_event_mapping(self):
        adapter = HarborAdapter()
        adapter.apply_event({"trial_id": TRIAL_A, "type": "trial.leased"})
        adapter.apply_event({"trial_id": TRIAL_A, "type": "trial.started"})
        self.assertEqual(adapter.machines[TRIAL_A].state, TrialState.RUNNING)
        self.assertEqual(event_to_machine_event("feedback.blocked"), "feedback_blocked")
        self.assertEqual(
            event_to_machine_event("observe.completed", {"needs_feedback": True}), "needs_feedback"
        )

    def test_harbor_error_mapping(self):
        self.assertIn("model_provider_failed", HARBOR_CAUSE_TO_ERROR_CLASS)
        error = error_from_harbor("model_provider_failed", message="5xx")
        self.assertEqual(error.error_class, ErrorClass.PROVIDER)
        unknown = error_from_harbor("some_new_cause")
        self.assertEqual(unknown.error_class, ErrorClass.ENVIRONMENT)
        adapter = HarborAdapter()
        adapter.machine_for(TRIAL_A)
        adapter.record_error(TRIAL_A, cause="sandbox_failed")
        self.assertEqual(len(adapter.errors), 1)


if __name__ == "__main__":
    unittest.main()
