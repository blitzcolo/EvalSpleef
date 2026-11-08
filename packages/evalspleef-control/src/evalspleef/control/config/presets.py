"""``presets.yaml``: model x hardware launch presets that people write and keep.

A preset records how one model runs on one hardware shape: the GPU type and count, the engine,
the launch arguments someone chose and checked, the context and concurrency it holds, free notes
and facts (weight size, precision), and a log of verification runs. ``models.yaml`` entries name a
preset with ``deployment.params.preset``; :func:`expand_preset` turns it into deployment params at
load time and records the preset id and a digest of its content, so a later edit of the preset
yields a different spec instead of silently changing a frozen one.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ._yaml import load_yaml_mapping
from .errors import ConfigError

__all__ = [
    "HardwareConfig",
    "PresetConfig",
    "PresetFileConfig",
    "VerificationRecord",
    "expand_preset",
    "load_presets",
    "preset_digest",
    "save_presets",
]

PRESET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*$")
MAX_GPUS = 8

#: Deployment params a preset fills; an explicit value in ``models.yaml`` wins over the preset.
_FILLED = ("gpu", "gpu_count", "engine", "engine_extra_args", "context_length", "max_concurrency")


class HardwareConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    """Where the hardware comes from, e.g. ``beam``."""
    gpu: str
    gpu_count: int = Field(default=1, ge=1, le=MAX_GPUS)
    vram_gb_per_gpu: float | None = Field(default=None, gt=0)
    sku: str | None = None
    """The provider's name for the machine shape, when it sells one."""


class VerificationRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    result: Literal["ok", "failed", "partial"]
    summary: str
    evidence: str | None = None


class PresetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    model: str
    """The Hugging Face repo (or vendor model id) the arguments were written for."""
    revision: str | None = None
    adapter: str | None = None
    """The deployment adapter the preset targets, e.g. ``beam-pod``; checked when set."""
    hardware: HardwareConfig
    engine: Literal["sglang", "vllm"]
    engine_image: str | None = None
    engine_args: list[str] = Field(default_factory=list)
    context_length: int | None = Field(default=None, gt=0)
    max_concurrency: int | None = Field(default=None, gt=0)
    deployment_params: dict[str, Any] = Field(default_factory=dict)
    """Further adapter params (cpu, memory, weights volume) merged under the filled ones."""
    facts: dict[str, str | int | float | bool] = Field(default_factory=dict)
    """External facts such as weight size, precision or native context."""
    notes: str = ""
    verified: list[VerificationRecord] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not PRESET_ID_RE.match(value):
            raise ValueError(
                f"preset id must be lowercase segments of [a-z0-9._-] joined by '/': {value!r}"
            )
        return value

    @field_validator("deployment_params")
    @classmethod
    def _no_shadowed_keys(cls, value: dict[str, Any]) -> dict[str, Any]:
        clash = sorted(set(value) & {*_FILLED, "preset", "preset_digest"})
        if clash:
            raise ValueError(f"set {clash} through the preset's own fields, not deployment_params")
        return value


class PresetFileConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    presets: list[PresetConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> PresetFileConfig:
        seen: set[str] = set()
        for preset in self.presets:
            if preset.id in seen:
                raise ValueError(f"preset id {preset.id!r} appears twice")
            seen.add(preset.id)
        return self

    def get(self, preset_id: str) -> PresetConfig | None:
        return next((preset for preset in self.presets if preset.id == preset_id), None)

    def search(
        self,
        text: str = "",
        *,
        model: str | None = None,
        gpu: str | None = None,
        gpu_count: int | None = None,
    ) -> list[PresetConfig]:
        """Presets matching every given filter; ``text`` matches any field, case-insensitively."""
        needle = text.lower()
        found = []
        for preset in self.presets:
            if model and model.lower() not in preset.model.lower():
                continue
            if gpu and gpu.lower() != preset.hardware.gpu.lower():
                continue
            if gpu_count is not None and gpu_count != preset.hardware.gpu_count:
                continue
            if needle and needle not in json.dumps(preset.model_dump(mode="json")).lower():
                continue
            found.append(preset)
        return found


def preset_digest(preset: PresetConfig) -> str:
    """sha256 over the fields that shape a deployment; notes and the verification log are left out."""
    shaping = preset.model_dump(mode="json", exclude={"notes", "verified", "facts"})
    canonical = json.dumps(shaping, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def expand_preset(
    params: Mapping[str, Any],
    preset: PresetConfig,
    *,
    adapter: str,
    model_ids: Iterable[str],
) -> dict[str, Any]:
    """Deployment params with the preset filled in under the explicit ones.

    Raises ValueError when the preset was written for another model or adapter.
    """
    names = {name.lower() for name in model_ids if name}
    if preset.model.lower() not in names:
        raise ValueError(f"preset {preset.id!r} is for {preset.model!r}, not {sorted(names)}")
    if preset.adapter is not None and preset.adapter != adapter:
        raise ValueError(
            f"preset {preset.id!r} targets adapter {preset.adapter!r}, not {adapter!r}"
        )
    filled: dict[str, Any] = {
        **preset.deployment_params,
        "gpu": preset.hardware.gpu,
        "gpu_count": preset.hardware.gpu_count,
        "engine": preset.engine,
        "engine_extra_args": list(preset.engine_args),
    }
    if preset.context_length is not None:
        filled["context_length"] = preset.context_length
    if preset.max_concurrency is not None:
        filled["max_concurrency"] = preset.max_concurrency
    explicit = {key: value for key, value in params.items() if key != "preset"}
    return {**filled, **explicit, "preset": preset.id, "preset_digest": preset_digest(preset)}


def load_presets(path: str | Path) -> PresetFileConfig:
    """Parse ``presets.yaml``; a missing file is an empty table."""
    file = str(path)
    if not Path(path).exists():
        return PresetFileConfig()
    document = load_yaml_mapping(path)
    try:
        return PresetFileConfig.model_validate(document)
    except Exception as exc:
        raise ConfigError.from_validation(file, exc) from exc


def save_presets(path: str | Path, presets: PresetFileConfig) -> None:
    """Write the table back, validated, in a stable field order. Comments are not kept."""
    document = {
        "presets": [preset.model_dump(mode="json", exclude_none=True) for preset in presets.presets]
    }
    PresetFileConfig.model_validate(document)
    Path(path).write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100), encoding="utf-8"
    )
