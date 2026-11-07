from __future__ import annotations

import pytest
from evalspleef_model_vpc_vllm.engine_args import (
    build_engine_args,
    select_profile,
)

from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.model import DeploymentSpec, HFSource, ModelSpec, VendorSource

REVISION = "a" * 40


def _spec(
    family: str = "qwen",
    repo: str = "Qwen/Qwen2.5-7B-Instruct",
    params: dict | None = None,
) -> ModelSpec:
    return ModelSpec(
        slug="test-model",
        spec_version=1,
        family=family,
        roles=frozenset({"target"}),
        source=HFSource(repo=repo, revision=REVISION, access="public"),
        deployment=DeploymentSpec(
            adapter="vpc-vllm",
            account="test",
            params=params or {},
        ),
        declared_context_tokens=32768,
        declared_max_output_tokens=2048,
        pricing_ref="vpc-vllm",
        license="apache-2.0",
        owner="evalspleef",
    )


def test_select_profile() -> None:
    assert select_profile("qwen").name == "qwen"
    assert select_profile("Qwen2.5").name == "qwen"
    assert select_profile("qwq").name == "qwen"
    assert select_profile("llama").name == "llama"
    assert select_profile("Llama-3.1").name == "llama"
    assert select_profile("deepseek").name == "deepseek"
    assert select_profile("glm").name == "glm"
    assert select_profile("unknown-exotic").name == "generic"


def test_build_engine_args_qwen() -> None:
    spec = _spec(family="qwen", repo="Qwen/Qwen2.5-7B-Instruct")
    args = build_engine_args(
        spec, served_model_name="test-model", tensor_parallel_size=2, port=8000
    )

    assert "--model" in args
    idx_model = args.index("--model")
    assert args[idx_model + 1] == "Qwen/Qwen2.5-7B-Instruct"

    assert "--revision" in args
    idx_rev = args.index("--revision")
    assert args[idx_rev + 1] == REVISION

    assert "--served-model-name" in args
    assert args[args.index("--served-model-name") + 1] == "test-model"

    assert "--tensor-parallel-size" in args
    assert args[args.index("--tensor-parallel-size") + 1] == "2"

    assert "--tool-call-parser" in args
    assert args[args.index("--tool-call-parser") + 1] == "hermes"
    assert "--reasoning-parser" in args
    assert args[args.index("--reasoning-parser") + 1] == "qwen3"


def test_build_engine_args_qwen_coder_variant() -> None:
    spec = _spec(family="qwen", repo="Qwen/Qwen2.5-Coder-32B-Instruct")
    args = build_engine_args(spec, served_model_name="coder-model")
    assert args[args.index("--tool-call-parser") + 1] == "qwen3_xml"
    assert "--reasoning-parser" not in args


def test_build_engine_args_llama() -> None:
    spec = _spec(family="llama", repo="meta-llama/Llama-3.1-8B-Instruct")
    args = build_engine_args(spec, served_model_name="llama-model")
    assert args[args.index("--tool-call-parser") + 1] == "llama3_json"


def test_build_engine_args_deepseek() -> None:
    spec = _spec(family="deepseek", repo="deepseek-ai/DeepSeek-V3")
    args = build_engine_args(spec, served_model_name="deepseek-model")
    assert args[args.index("--tool-call-parser") + 1] == "deepseek_v3"
    assert args[args.index("--reasoning-parser") + 1] == "deepseek_r1"
    assert "--trust-remote-code" in args


def test_build_engine_args_overrides() -> None:
    spec = _spec(
        family="qwen",
        repo="Qwen/Qwen2.5-7B",
        params={
            "engine_overrides": {
                "tool_call_parser": "custom_parser",
                "quantization": "fp8",
                "extra_args": ["--disable-log-requests", "--gpu-memory-utilization", "0.95"],
            }
        },
    )
    args = build_engine_args(spec, served_model_name="overridden")
    assert args[args.index("--tool-call-parser") + 1] == "custom_parser"
    assert args[args.index("--quantization") + 1] == "fp8"
    assert "--disable-log-requests" in args
    assert args[args.index("--gpu-memory-utilization") + 1] == "0.95"


def test_build_engine_args_rejects_non_hf() -> None:
    spec = ModelSpec(
        slug="vendor-model",
        spec_version=1,
        family="generic",
        roles=frozenset({"target"}),
        source=VendorSource(vendor="openai", model_id="gpt-4o"),
        deployment=DeploymentSpec(adapter="vpc-vllm", account="test"),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="ref",
        license="prop",
        owner="test",
    )
    with pytest.raises(CapabilityNotSupported):
        build_engine_args(spec, served_model_name="vendor-model")
