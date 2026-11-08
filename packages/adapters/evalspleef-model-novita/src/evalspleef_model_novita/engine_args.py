"""vLLM and SGLang argument generator driven by the per-family YAML profiles (TECH-DESIGN 6.6.3/4).

The generated command pins the model to a mirror repo and a commit sha, so the engine can
never pull a moving revision. Profiles live in ``engine_profiles/<family>.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Any, Literal

import yaml

from evalspleef.spi.errors import CapabilityNotSupported
from evalspleef.spi.model import HFSource, ModelSpec

PROFILE_NAMES = ("qwen", "llama", "deepseek", "glm", "generic")

Engine = Literal["vllm", "sglang"]


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
    sglang: ProfileVariant | None = None
    """SGLang parser names; the engine names its parsers differently from vLLM."""


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
        sglang=_sglang_names(raw.get("sglang")),
    )


def _sglang_names(raw: object) -> ProfileVariant | None:
    if not isinstance(raw, Mapping):
        return None
    return ProfileVariant(
        match=(),
        tool_call_parser=_optional_str(raw.get("tool_call_parser")),
        reasoning_parser=_optional_str(raw.get("reasoning_parser")),
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
            "serverless vLLM deployments need a Hugging Face source",
            detail={"source": type(spec.source).__name__},
        )
    return spec.source


def build_engine_args(
    spec: ModelSpec,
    *,
    served_model_name: str,
    tensor_parallel_size: int = 1,
    port: int = 8000,
    engine: Engine = "vllm",
) -> list[str]:
    """Engine argv for ``spec``: vLLM without the executable (the image entrypoint runs
    it), SGLang with ``python3 -m sglang.launch_server`` (its images have none).

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
    if engine == "sglang":
        if profile.sglang is None and "tool_call_parser" not in overrides:
            raise CapabilityNotSupported(
                f"engine profile {profile.name!r} has no SGLang parser names; "
                "set engine_overrides.tool_call_parser",
                detail={"engine": engine, "profile": profile.name},
            )
        names = profile.sglang or ProfileVariant(
            match=(), tool_call_parser=None, reasoning_parser=None
        )
        tool_parser, reasoning_parser = names.tool_call_parser, names.reasoning_parser
    tool_parser = overrides.get("tool_call_parser", tool_parser)
    reasoning_parser = overrides.get("reasoning_parser", reasoning_parser)
    quantization = overrides.get("quantization", quantization)
    extra_args.extend(str(arg) for arg in overrides.get("extra_args") or [])

    if engine == "sglang":
        return _sglang_args(
            source,
            served_model_name=served_model_name,
            port=port,
            context=spec.declared_context_tokens,
            tensor_parallel_size=tensor_parallel_size,
            quantization=quantization,
            tool_parser=tool_parser,
            reasoning_parser=reasoning_parser,
            extra_args=extra_args,
        )
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


def _sglang_args(
    source: HFSource,
    *,
    served_model_name: str,
    port: int,
    context: int,
    tensor_parallel_size: int,
    quantization: object,
    tool_parser: object,
    reasoning_parser: object,
    extra_args: list[str],
) -> list[str]:
    args = [
        "python3",
        "-m",
        "sglang.launch_server",
        "--model-path",
        source.mirror_repo or source.repo,
        "--revision",
        source.revision,
        "--served-model-name",
        served_model_name,
        "--host",
        "0.0.0.0",
        "--port",
        str(port),
        "--context-length",
        str(context),
        "--tp-size",
        str(tensor_parallel_size),
    ]
    if quantization:
        args += ["--quantization", str(quantization)]
    if tool_parser:
        args += ["--tool-call-parser", str(tool_parser)]
    if reasoning_parser:
        args += ["--reasoning-parser", str(reasoning_parser)]
    return args + extra_args
