"""Trial version matrix (PRD FR-MOD-09, TECH-DESIGN section 11).

``trial.versions`` records what a trial actually ran against.  A change in any
of these fields means the result is not directly comparable with an earlier
one, which is why the batch freeze stores the whole matrix (PRD FR-FLOW-01).

The v2 fields come from ``schemas/trial.schema.json``; the v3 fields come from
PRD FR-MOD-09 and TECH-DESIGN section 11.  Only ``inference_engine`` uses the
PRD's explicit ``"unknown"`` sentinel: Novita does not expose which engine
(vLLM or SGLang) it started, so the field must still be recorded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["INFERENCE_ENGINE_UNKNOWN", "TrialVersions"]

#: Recorded when the provider does not expose the inference engine (PRD FR-MOD-09).
INFERENCE_ENGINE_UNKNOWN = "unknown"


def _optional_str(data: Mapping[str, Any], key: str) -> str | None:
    value = data.get(key)
    return None if value is None else str(value)


def _optional_int(data: Mapping[str, Any], key: str) -> int | None:
    value = data.get(key)
    return None if value is None else int(value)


@dataclass(frozen=True)
class TrialVersions:
    """The version matrix of one trial.

    The seven required attributes are the v2 contract from
    ``schemas/trial.schema.json``.  Everything after them is additive; ``None``
    means "not applicable for this trial" (for example vendor-API models have
    no Hugging Face repository).
    """

    # v2 required fields (schemas/trial.schema.json $defs.versions).
    task_version: str
    model_version: str
    shell_version: str
    image_digest: str
    rubric_version: str
    grader_version: str
    freeze_hash: str

    # v2 optional fields.
    verifier_image_digest: str | None = None
    harbor_version: str | None = None
    evalkit_version: str | None = None
    judge_prompt_version: str | None = None

    # v3 model and deployment fields (PRD FR-MOD-09).
    model_source: str | None = None
    hf_repo: str | None = None
    hf_revision: str | None = None
    hf_mirror_repo: str | None = None
    deployment_adapter: str | None = None
    deployment_id: str | None = None
    endpoint_base_url_hash: str | None = None
    endpoint_id: str | None = None
    probe_report_id: str | None = None
    inference_engine: str = INFERENCE_ENGINE_UNKNOWN
    gpu_type: str | None = None
    replicas_planned: int | None = None
    quantization: str | None = None
    context_length_effective: int | None = None
    chat_template_sha256: str | None = None
    gateway_version: str | None = None

    # v3 sandbox and runtime fields (TECH-DESIGN section 11).
    sandbox_adapter: str | None = None
    sandbox_adapter_version: str | None = None
    provider_instance: str | None = None
    template_digest: str | None = None
    hostd_version: str | None = None
    runner_mode: str | None = None
    harbor: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise every field; absent optional values stay ``None``."""
        return {
            "task_version": self.task_version,
            "model_version": self.model_version,
            "shell_version": self.shell_version,
            "image_digest": self.image_digest,
            "rubric_version": self.rubric_version,
            "grader_version": self.grader_version,
            "freeze_hash": self.freeze_hash,
            "verifier_image_digest": self.verifier_image_digest,
            "harbor_version": self.harbor_version,
            "evalkit_version": self.evalkit_version,
            "judge_prompt_version": self.judge_prompt_version,
            "model_source": self.model_source,
            "hf_repo": self.hf_repo,
            "hf_revision": self.hf_revision,
            "hf_mirror_repo": self.hf_mirror_repo,
            "deployment_adapter": self.deployment_adapter,
            "deployment_id": self.deployment_id,
            "endpoint_base_url_hash": self.endpoint_base_url_hash,
            "endpoint_id": self.endpoint_id,
            "probe_report_id": self.probe_report_id,
            "inference_engine": self.inference_engine,
            "gpu_type": self.gpu_type,
            "replicas_planned": self.replicas_planned,
            "quantization": self.quantization,
            "context_length_effective": self.context_length_effective,
            "chat_template_sha256": self.chat_template_sha256,
            "gateway_version": self.gateway_version,
            "sandbox_adapter": self.sandbox_adapter,
            "sandbox_adapter_version": self.sandbox_adapter_version,
            "provider_instance": self.provider_instance,
            "template_digest": self.template_digest,
            "hostd_version": self.hostd_version,
            "runner_mode": self.runner_mode,
            "harbor": self.harbor,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrialVersions:
        """Build a matrix from a ``trial.versions`` mapping.

        Required v2 fields raise :class:`KeyError` when absent; unknown extra
        keys are ignored so a later schema revision does not break older code.
        """
        return cls(
            task_version=str(data["task_version"]),
            model_version=str(data["model_version"]),
            shell_version=str(data["shell_version"]),
            image_digest=str(data["image_digest"]),
            rubric_version=str(data["rubric_version"]),
            grader_version=str(data["grader_version"]),
            freeze_hash=str(data["freeze_hash"]),
            verifier_image_digest=_optional_str(data, "verifier_image_digest"),
            harbor_version=_optional_str(data, "harbor_version"),
            evalkit_version=_optional_str(data, "evalkit_version"),
            judge_prompt_version=_optional_str(data, "judge_prompt_version"),
            model_source=_optional_str(data, "model_source"),
            hf_repo=_optional_str(data, "hf_repo"),
            hf_revision=_optional_str(data, "hf_revision"),
            hf_mirror_repo=_optional_str(data, "hf_mirror_repo"),
            deployment_adapter=_optional_str(data, "deployment_adapter"),
            deployment_id=_optional_str(data, "deployment_id"),
            endpoint_base_url_hash=_optional_str(data, "endpoint_base_url_hash"),
            endpoint_id=_optional_str(data, "endpoint_id"),
            probe_report_id=_optional_str(data, "probe_report_id"),
            inference_engine=(
                INFERENCE_ENGINE_UNKNOWN
                if data.get("inference_engine") is None
                else str(data["inference_engine"])
            ),
            gpu_type=_optional_str(data, "gpu_type"),
            replicas_planned=_optional_int(data, "replicas_planned"),
            quantization=_optional_str(data, "quantization"),
            context_length_effective=_optional_int(data, "context_length_effective"),
            chat_template_sha256=_optional_str(data, "chat_template_sha256"),
            gateway_version=_optional_str(data, "gateway_version"),
            sandbox_adapter=_optional_str(data, "sandbox_adapter"),
            sandbox_adapter_version=_optional_str(data, "sandbox_adapter_version"),
            provider_instance=_optional_str(data, "provider_instance"),
            template_digest=_optional_str(data, "template_digest"),
            hostd_version=_optional_str(data, "hostd_version"),
            runner_mode=_optional_str(data, "runner_mode"),
            harbor=_optional_str(data, "harbor"),
        )
