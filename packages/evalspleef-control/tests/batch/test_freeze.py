"""Freeze record: the version matrix hash changes with every frozen input and nothing else."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest

from evalspleef.control.batch import FreezeRecord
from evalspleef.spi.caps import Isolation

OTHER_DIGEST = "sha256:" + "c" * 64

#: Each frozen input of FR-FLOW-01 -> a change to the planner inputs that must alter exactly it.
CHANGES: dict[str, Callable[[Any], dict[str, Any]]] = {
    "probe_report_ids": lambda h: {
        "models": [replace(m, probe_report_id="pr_other") for m in h.models]
    },
    "model_revisions": lambda h: {"models": [replace(m, revision="b" * 40) for m in h.models]},
    "model_spec_versions": lambda h: {"models": [replace(m, spec_version=2) for m in h.models]},
    "template_digests": lambda h: {
        "template_digests": {t.template.ref: {"docker-a": OTHER_DIGEST} for t in h.tasks}
    },
    "gateway_version": lambda h: {"gateway_version": "gw-2.0"},
    "price_table_version": lambda h: {"price_table_version": "prices-2026-12"},
    "runner_versions": lambda h: {"runner_versions": {"terminus": "0.4.0"}},
    "isolation_profiles": lambda h: {
        "profiles": {
            "docker-a": replace(h.profiles["docker-a"], declared_isolation=Isolation.L2),
        }
    },
    "task_versions": lambda h: {"tasks": [replace(t, version="1.1.0") for t in h.tasks]},
    "judges": lambda h: {
        "judges": {"judge-a": {"spec_version": 2, "endpoint_id": "ep_judge-a_1", "family": "f"}}
    },
}


@pytest.mark.parametrize("frozen_input", list(CHANGES))
async def test_changing_a_frozen_input_changes_the_hash(build, frozen_input: str) -> None:
    h = await build(n_models=2, n_tasks=2)

    changed = h.refreeze(**CHANGES[frozen_input](h))

    assert changed.version_matrix_hash != h.freeze.version_matrix_hash
    assert changed.diff(h.freeze) == (frozen_input,)


async def test_rebuilding_from_the_same_inputs_gives_the_same_hash(build) -> None:
    h = await build(n_models=2, n_tasks=2)

    again = h.refreeze()

    assert again.version_matrix_hash == h.freeze.version_matrix_hash
    assert again.diff(h.freeze) == ()
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", h.freeze.version_matrix_hash)


async def test_hash_ignores_batch_id_timestamp_and_key_order(build) -> None:
    h = await build(n_models=2)
    base = h.freeze
    shuffled = replace(
        base,
        batch_id="2026-12-b9",
        frozen_at="2030-01-01T00:00:00Z",
        model_spec_versions=dict(reversed(list(base.model_spec_versions.items()))),
        probe_report_ids=dict(reversed(list(base.probe_report_ids.items()))),
    )

    assert shuffled.version_matrix_hash == base.version_matrix_hash


async def test_record_serializes_its_hash_next_to_the_matrix(build) -> None:
    h = await build()

    data = h.freeze.to_dict()

    assert data["version_matrix_hash"] == h.freeze.version_matrix_hash
    assert data["gateway_version"] == "gw-1.0"
    assert data["batch_id"] == "2026-11-b1"


async def test_only_inputs_of_runnable_cells_are_frozen(build, task_factory) -> None:
    runnable_only = await build(tasks=[task_factory("web-001")])
    with_skipped = await build(tasks=[task_factory("web-001"), task_factory("web-002", gpu=True)])

    assert with_skipped.freeze.task_versions == {"web-001": "1.0.0"}
    assert with_skipped.freeze.version_matrix_hash == runnable_only.freeze.version_matrix_hash


def test_template_digest_change_is_visible_in_the_matrix() -> None:
    record = FreezeRecord(
        batch_id="2026-11-b1",
        probe_report_ids={},
        model_revisions={},
        model_spec_versions={},
        template_digests={"img": {"docker-a": "sha256:" + "a" * 64}},
        gateway_version="gw",
        price_table_version="p",
        runner_versions={},
        isolation_profiles={},
        task_versions={},
    )
    other = replace(record, template_digests={"img": {"docker-a": OTHER_DIGEST}})

    assert record.diff(other) == ("template_digests",)
    assert record.version_matrix_hash != other.version_matrix_hash
