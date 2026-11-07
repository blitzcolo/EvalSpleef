"""vLLM argument generator driven by per-family YAML profiles (TECH-DESIGN section 6.6.4).

Generates command arguments for vLLM or SGLang that pin the model to a mirror repo and a commit
sha, preventing moving revisions. Profiles live in ``engine_profiles/<family>.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.model import HFSource, ModelSpec

PROFILE_NAMES = ("qwen", "llama", "deepseek", "glm", "generic")


@dataclass(frozen=True)
class ProfileVariant:
    match: tuple[str, ...]
    tool_call_parser: str | None
    reasoning_parser: str | None


@dataclass(frozen=True)
class EngineProfile:
    name: str
    match: tuple[str, ...]
    tool_call_parser: str | None
    reasoning_parser: str | None
    quantization: str | None
    extra_args: tuple[str, ...]
    variants: tuple[ProfileVariant, ...]


def _optional_str(value: object) -> str | None:
    return None if value is None else str(value)


def _parse_profile(raw: Mapping[str, Any]) -> EngineProfile:
    variants = tuple(
        ProfileVariant(
            match=tuple(str(token).lower() for token in variant["match"]),
            tool_call_parser=_optional_str(variant.get("tool_call_parser")),
            reasoning_parser=_optional_str(variant.get("reasoning_parser")),
        )
        for variant in raw.get("variants") or []
    )
    return EngineProfile(
        name=str(raw["name"]),
        match=tuple(str(token).lower() for token in raw.get("match") or []),
        tool_call_parser=_optional_str(raw.get("tool_call_parser")),
        reasoning_parser=_optional_str(raw.get("reasoning_parser")),
        quantization=_optional_str(raw.get("quantization")),
        extra_args=tuple(str(arg) for arg in raw.get("extra_args") or []),
        variants=variants,
    )


def load_profile(name: str) -> EngineProfile:
    if name not in PROFILE_NAMES:
        raise KeyError(f"unknown engine profile {name!r}; known: {', '.join(PROFILE_NAMES)}")
    text = (resources.files(__package__) / "engine_profiles" / f"{name}.yaml").read_text("utf-8")
    raw = yaml.safe_load(text)
    if not isinstance(raw, Mapping):
        raise ValueError(f"engine profile {name!r} is not a mapping")
    return _parse_profile(raw)


def select_profile(family: str) -> EngineProfile:
    """First family profile whose match token occurs in ``family``; ``generic`` otherwise."""
    lowered = family.lower()
    for name in PROFILE_NAMES[:-1]:
        profile = load_profile(name)
        if any(token in lowered for token in profile.match):
            return profile
    return load_profile("generic")


def _require_hf(spec: ModelSpec) -> HFSource:
    if not isinstance(spec.source, HFSource):
        raise CapabilityNotSupported(
            "vpc-vllm deployments need a Hugging Face source",
            detail={"source": type(spec.source).__name__},
        )
    return spec.source


def build_engine_args(
    spec: ModelSpec,
    *,
    served_model_name: str,
    tensor_parallel_size: int = 1,
    port: int = 8000,
) -> list[str]:
    """vLLM argv (without the executable) for ``spec``.

    ``spec.deployment.params["engine_overrides"]`` may set ``tool_call_parser``,
    ``reasoning_parser``, ``quantization`` (``None`` switches one off) and ``extra_args``.
    """
    source = _require_hf(spec)
    profile = select_profile(spec.family)
    haystack = f"{spec.family} {source.repo}".lower()
    tool_parser = profile.tool_call_parser
    reasoning_parser = profile.reasoning_parser
    for variant in profile.variants:
        if any(token in haystack for token in variant.match):
            tool_parser = variant.tool_call_parser
            reasoning_parser = variant.reasoning_parser
            break
    quantization = profile.quantization
    extra_args = list(profile.extra_args)

    raw_overrides = spec.deployment.params.get("engine_overrides")
    overrides: Mapping[str, Any] = raw_overrides if isinstance(raw_overrides, Mapping) else {}
    tool_parser = overrides.get("tool_call_parser", tool_parser)
    reasoning_parser = overrides.get("reasoning_parser", reasoning_parser)
    quantization = overrides.get("quantization", quantization)
    extra_args.extend(str(arg) for arg in overrides.get("extra_args") or [])

    args = [
        "--model",
        source.mirror_repo or source.repo,
        "--revision",
        source.revision,
        "--served-model-name",
        served_model_name,
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--max-model-len",
        str(spec.declared_context_tokens),
        "--tensor-parallel-size",
        str(tensor_parallel_size),
    ]
    if quantization:
        args += ["--quantization", str(quantization)]
    if tool_parser:
        args += ["--enable-auto-tool-choice", "--tool-call-parser", str(tool_parser)]
    if reasoning_parser:
        args += ["--reasoning-parser", str(reasoning_parser)]
    return args + extra_args
