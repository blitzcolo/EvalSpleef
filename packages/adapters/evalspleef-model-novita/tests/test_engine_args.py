from __future__ import annotations

from collections.abc import Callable

import pytest
from evalspleef_model_novita.engine_args import PROFILE_NAMES, build_engine_args, select_profile

from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.model import DeploymentSpec, ModelSpec, VendorSource

SpecFactory = Callable[..., ModelSpec]
REPO = "evalspleef-conformance/public-model"
SHA = "1a2b3c4d" * 5


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


@pytest.mark.parametrize(
    ("family", "profile", "tool_parser", "reasoning_parser"),
    [
        ("qwen3", "qwen", "hermes", "qwen3"),
        ("llama3.1", "llama", "llama3_json", None),
        ("deepseek-r1", "deepseek", "deepseek_v3", "deepseek_r1"),
        ("glm-4.5", "glm", "glm45", "glm45"),
        ("mistral", "generic", None, None),
    ],
)
def test_family_profile_selects_parsers(
    make_spec: SpecFactory,
    family: str,
    profile: str,
    tool_parser: str | None,
    reasoning_parser: str | None,
) -> None:
    spec = make_spec(family=family)
    assert select_profile(family).name == profile
    args = build_engine_args(spec, served_model_name="served")
    assert _flag(args, "--tool-call-parser") == tool_parser
    assert _flag(args, "--reasoning-parser") == reasoning_parser
    assert ("--enable-auto-tool-choice" in args) is (tool_parser is not None)


def test_every_profile_file_loads() -> None:
    for name in PROFILE_NAMES:
        assert select_profile(name).name in PROFILE_NAMES


def test_variant_overrides_family_parser(make_spec: SpecFactory) -> None:
    spec = make_spec(family="glm-4.7", repo="zai-org/GLM-4.7")
    assert _flag(build_engine_args(spec, served_model_name="s"), "--tool-call-parser") == "glm47"


def test_model_is_pinned_to_mirror_and_revision(make_spec: SpecFactory) -> None:
    spec = make_spec(mirror_repo="org/mirror-x-1a2b3c4")
    args = build_engine_args(spec, served_model_name="served", tensor_parallel_size=2, port=9000)
    assert _flag(args, "--model") == "org/mirror-x-1a2b3c4"
    assert _flag(args, "--revision") == SHA
    assert _flag(args, "--max-model-len") == "8192"
    assert _flag(args, "--tensor-parallel-size") == "2"
    assert _flag(args, "--port") == "9000"
    assert _flag(args, "--served-model-name") == "served"


def test_without_mirror_the_source_repo_is_used(make_spec: SpecFactory) -> None:
    assert _flag(build_engine_args(make_spec(), served_model_name="s"), "--model") == REPO


def test_overrides_set_and_disable_flags(make_spec: SpecFactory) -> None:
    spec = make_spec(
        engine_overrides={
            "quantization": "fp8",
            "reasoning_parser": None,
            "extra_args": ["--max-num-seqs", "64"],
        }
    )
    args = build_engine_args(spec, served_model_name="s")
    assert _flag(args, "--quantization") == "fp8"
    assert "--reasoning-parser" not in args
    assert args[-2:] == ["--max-num-seqs", "64"]


def test_a_non_hf_source_is_rejected(make_spec: SpecFactory) -> None:
    base = make_spec()
    spec = ModelSpec(
        slug=base.slug,
        spec_version=1,
        family="qwen3",
        roles=base.roles,
        source=VendorSource(vendor="novita", model_id="qwen/qwen3"),
        deployment=DeploymentSpec(adapter="novita", account="acme"),
        declared_context_tokens=8192,
        declared_max_output_tokens=1024,
        pricing_ref="x",
        license="x",
        owner="x",
    )
    with pytest.raises(CapabilityNotSupported):
        build_engine_args(spec, served_model_name="s")
