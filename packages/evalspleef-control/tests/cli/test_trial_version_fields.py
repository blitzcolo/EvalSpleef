from __future__ import annotations

from dataclasses import MISSING
from typing import Any

from evalspleef.control.cli.context import deployment_version_fields
from evalspleef.core.versions import TrialVersions
from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec, VendorSource

SHA = "a" * 40
IMAGE = "lmsysorg/sglang:latest@sha256:" + "f" * 64


def _spec(source: HFSource | VendorSource, **params: Any) -> ModelSpec:
    return ModelSpec(
        slug="m",
        spec_version=1,
        family="f",
        roles=frozenset({"target"}),
        source=source,
        deployment=DeploymentSpec(adapter="beam-pod", account="a", params=params),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="p",
        license="mit",
        owner="o",
    )


def test_a_self_served_model_records_engine_gpu_and_chat_template() -> None:
    source = HFSource(
        repo="o/r",
        revision=SHA,
        access="public",
        files_sha256={"chat_template.jinja": "sha256:tpl", "tokenizer_config.json": "sha256:tok"},
    )
    fields = deployment_version_fields(
        _spec(source, engine="sglang", gpu="RTX5090"), {"image": IMAGE}
    )
    assert fields == {
        "inference_engine": f"sglang {IMAGE}",
        "gpu_type": "RTX5090",
        "chat_template_sha256": "sha256:tpl",
    }
    versions = TrialVersions.from_dict({**_v2_required(), **fields})
    assert versions.chat_template_sha256 == "sha256:tpl"


def test_a_template_inside_the_tokenizer_config_is_hashed_through_that_file() -> None:
    source = HFSource(
        repo="o/r",
        revision=SHA,
        access="public",
        files_sha256={"tokenizer_config.json": "sha256:tok"},
    )
    fields = deployment_version_fields(_spec(source), {})
    assert fields["chat_template_sha256"] == "sha256:tok"
    assert fields["inference_engine"] == "unknown"


def test_a_hosted_api_model_records_nothing_it_cannot_know() -> None:
    fields = deployment_version_fields(_spec(VendorSource(vendor="v", model_id="x")), {})
    assert fields == {"inference_engine": "unknown", "gpu_type": None, "chat_template_sha256": None}


def _v2_required() -> dict[str, Any]:
    return {
        name: "x"
        for name, field in TrialVersions.__dataclass_fields__.items()
        if field.default is MISSING and field.default_factory is MISSING
    }
