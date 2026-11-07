"""Trial version matrix (PRD FR-MOD-09, TECH-DESIGN section 11)."""

from __future__ import annotations

import pytest

from evalspleef.core.versions import INFERENCE_ENGINE_UNKNOWN, TrialVersions

REQUIRED_V2 = {
    "task_version": "1.2.0",
    "model_version": "qwen3-32b-2026-10",
    "shell_version": "claude-code-2.1.0",
    "image_digest": "sha256:" + "a" * 64,
    "rubric_version": "1.0.0",
    "grader_version": "evalkit-0.4.1",
    "freeze_hash": "b" * 64,
}

FULL = {
    **REQUIRED_V2,
    "verifier_image_digest": "sha256:" + "c" * 64,
    "harbor_version": "0.9.0",
    "evalkit_version": "0.4.1",
    "judge_prompt_version": "judge-v3",
    "model_source": "huggingface",
    "hf_repo": "Qwen/Qwen3-32B",
    "hf_revision": "d" * 40,
    "hf_mirror_repo": "evalspleef/mirror-qwen3-32b-1a2b3c4",
    "deployment_adapter": "novita-dedicated",
    "deployment_id": "dep_qwen3-32b_7c1e",
    "endpoint_base_url_hash": "e" * 64,
    "endpoint_id": "ep_qwen3-32b_7c1e",
    "probe_report_id": "probe_01J0000000000000000000000",
    "inference_engine": "vllm/0.11.0",
    "gpu_type": "h200",
    "replicas_planned": 2,
    "quantization": "fp8",
    "context_length_effective": 131072,
    "chat_template_sha256": "f" * 64,
    "gateway_version": "0.3.0",
    "sandbox_adapter": "docker",
    "sandbox_adapter_version": "0.2.1",
    "provider_instance": "docker-local",
    "template_digest": "sha256:" + "1" * 64,
    "hostd_version": "0.1.0",
    "runner_mode": "remote-tools",
    "harbor": "web-001-separate",
}


def test_round_trip_keeps_every_field() -> None:
    versions = TrialVersions.from_dict(FULL)
    assert versions.to_dict() == FULL
    assert TrialVersions.from_dict(versions.to_dict()) == versions


def test_inference_engine_defaults_to_unknown() -> None:
    versions = TrialVersions(**REQUIRED_V2)
    assert versions.inference_engine == INFERENCE_ENGINE_UNKNOWN == "unknown"
    assert versions.to_dict()["inference_engine"] == "unknown"


def test_from_dict_records_unknown_engine_for_null_or_missing() -> None:
    without_engine = {key: value for key, value in REQUIRED_V2.items()}
    assert TrialVersions.from_dict(without_engine).inference_engine == "unknown"
    null_engine = TrialVersions.from_dict({**REQUIRED_V2, "inference_engine": None})
    assert null_engine.inference_engine == "unknown"


def test_nullable_v3_fields_stay_null() -> None:
    versions = TrialVersions.from_dict(REQUIRED_V2)
    payload = versions.to_dict()
    assert payload["hf_mirror_repo"] is None
    assert payload["endpoint_id"] is None
    assert payload["harbor"] is None
    assert payload["replicas_planned"] is None
    assert payload["context_length_effective"] is None


def test_numeric_fields_are_coerced() -> None:
    versions = TrialVersions.from_dict(
        {**REQUIRED_V2, "replicas_planned": "3", "context_length_effective": 65536}
    )
    assert versions.replicas_planned == 3
    assert versions.context_length_effective == 65536


def test_missing_required_field_raises() -> None:
    incomplete = dict(REQUIRED_V2)
    del incomplete["freeze_hash"]
    with pytest.raises(KeyError, match="freeze_hash"):
        TrialVersions.from_dict(incomplete)
