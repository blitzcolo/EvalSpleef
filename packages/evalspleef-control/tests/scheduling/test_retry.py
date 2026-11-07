"""Retry numbering: a retry is a new record with an incremented index."""

from __future__ import annotations

import pytest

from evalspleef.control.scheduling import MAX_INFRA_RETRIES, next_trial_id, retry_decision

TRIAL = "web-001.alpha.terminus.e2.t01"


def test_next_trial_id_increments_only_the_index() -> None:
    assert next_trial_id(TRIAL) == "web-001.alpha.terminus.e2.t02"
    assert next_trial_id("web-001.alpha.terminus.e2.t09") == "web-001.alpha.terminus.e2.t10"


def test_next_trial_id_rejects_malformed_ids() -> None:
    with pytest.raises(ValueError):
        next_trial_id("not-a-trial-id")


def test_a_chain_gets_three_retries_and_then_stops() -> None:
    trial_id = TRIAL
    ids = [trial_id]
    for attempt in range(MAX_INFRA_RETRIES):
        decision = retry_decision(trial_id, attempt)
        assert decision.retry
        assert decision.attempt == attempt + 1
        assert decision.next_trial_id is not None
        trial_id = decision.next_trial_id
        ids.append(trial_id)

    assert ids == [f"web-001.alpha.terminus.e2.t0{n}" for n in (1, 2, 3, 4)]
    final = retry_decision(trial_id, MAX_INFRA_RETRIES)
    assert not final.retry
    assert final.next_trial_id is None
