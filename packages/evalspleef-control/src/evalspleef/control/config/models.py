"""``models.yaml``: model registry entries (TECH-DESIGN section 14.1)."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from evalspleef.spi.model import (
    DeploymentSpec,
    HFSource,
    ModelRole,
    ModelSpec,
    VendorSource,
)

from ._yaml import load_yaml_mapping
from .errors import ConfigError
from .presets import expand_preset, load_presets

__all__ = [
    "HFSourceConfig",
    "ModelConfig",
    "ModelFileConfig",
    "VendorSourceConfig",
    "load_hf_token_expiry",
    "load_models",
]

#: A pinned commit sha, exactly 40 lowercase hex characters (FR-MOD-02).
REVISION_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: Model slugs are lowercase, vendor-prefix-free, hyphenated (PRD appendix A.1).
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Hugging Face repository ids are "org/name" with no leading/trailing dash or dot segments.
REPO_RE = re.compile(r"^[\w.-]+/[\w.-]+$")

#: Schemes a SecretRef may use in configuration files.
_SECRET_SCHEMES = ("vault://", "env://", "file://")


def _check_secret_ref(value: str) -> str:
    if not value.startswith(_SECRET_SCHEMES):
        raise ValueError(
            f"secret reference must use one of {_SECRET_SCHEMES}, got scheme in {value!r}"
        )
    return value


def _check_repo(value: str) -> str:
    if not REPO_RE.match(value):
        raise ValueError(f"Hugging Face repo must look like 'org/name': {value!r}")
    return value


class HFSourceConfig(BaseModel):
    """An HF-hosted model source. ``revision`` must pin a commit sha."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["huggingface"] = "huggingface"
    repo: str
    revision: str
    access: Literal["public", "gated", "private"]
    token_ref: str | None = None
    #: Expiry date of the token behind ``token_ref``, or ``never``. Hugging Face does
    #: not report it through its API, so the operator records it here (TECH-DESIGN
    #: 6.5); the batch freeze needs it to lie past the planned end plus 7 days.
    token_expires: date | Literal["never"] | None = None
    mirror: bool = False
    mirror_repo: str | None = None
    adapter_repo: str | None = None
    files_sha256: dict[str, str] = Field(default_factory=dict)
    #: True when ``revision`` names a branch or tag that the registration CLI
    #: resolves to a sha and writes back; frozen specs must not set this.
    unresolved_branch: bool = False

    @field_validator("repo", "mirror_repo")
    @classmethod
    def _repo_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_repo(value)

    @field_validator("token_ref")
    @classmethod
    def _token_ref_scheme(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _check_secret_ref(value)

    @model_validator(mode="after")
    def _expiry_needs_a_token(self) -> HFSourceConfig:
        if self.token_expires is not None and self.token_ref is None:
            raise ValueError("token_expires is set but there is no token_ref it belongs to")
        return self

    @model_validator(mode="after")
    def _revision_is_pinned(self) -> HFSourceConfig:
        if REVISION_SHA_RE.match(self.revision):
            return self
        if self.unresolved_branch:
            return self
        raise ValueError(
            f"revision must be a 40-hex commit sha, got {self.revision!r}; "
            "set unresolved_branch: true only while a branch or tag is still to be resolved"
        )


class VendorSourceConfig(BaseModel):
    """An official-vendor API model source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["vendor"]
    vendor: str
    model_id: str


class DeploymentConfig(BaseModel):
    """Adapter, account (provider instance) and adapter-private parameters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    adapter: str
    account: str
    params: dict[str, Any] = Field(default_factory=dict)


class ModelConfig(BaseModel):
    """One entry of ``models.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str
    spec_version: int = Field(default=1, ge=1)
    family: str
    roles: list[ModelRole]
    source: HFSourceConfig | VendorSourceConfig = Field(discriminator="type")
    deployment: DeploymentConfig
    declared_context_tokens: int = Field(gt=0)
    declared_max_output_tokens: int = Field(gt=0)
    pricing_ref: str
    license: str
    owner: str

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, value: str) -> str:
        if not SLUG_RE.match(value):
            raise ValueError(f"model slug must be lowercase hyphenated (PRD A.1): {value!r}")
        return value

    @field_validator("family")
    @classmethod
    def _family_nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("family must not be empty")
        return value

    @field_validator("roles")
    @classmethod
    def _roles_nonempty(cls, value: list[ModelRole]) -> list[ModelRole]:
        if not value:
            raise ValueError("roles must list at least one of target/judge/user")
        if len(set(value)) != len(value):
            raise ValueError("roles must not repeat entries")
        return value

    def to_model_spec(self) -> ModelSpec:
        """Convert to the immutable SPI dataclass (TECH-DESIGN section 6.1)."""
        source: HFSource | VendorSource
        if isinstance(self.source, HFSourceConfig):
            source = HFSource(
                repo=self.source.repo,
                revision=self.source.revision,
                access=self.source.access,
                token_ref=self.source.token_ref,
                mirror_repo=self.source.mirror_repo,
                adapter_repo=self.source.adapter_repo,
                files_sha256=dict(self.source.files_sha256),
            )
        else:
            source = VendorSource(vendor=self.source.vendor, model_id=self.source.model_id)
        return ModelSpec(
            slug=self.slug,
            spec_version=self.spec_version,
            family=self.family,
            roles=frozenset(self.roles),
            source=source,
            deployment=DeploymentSpec(
                adapter=self.deployment.adapter,
                account=self.deployment.account,
                params=dict(self.deployment.params),
            ),
            declared_context_tokens=self.declared_context_tokens,
            declared_max_output_tokens=self.declared_max_output_tokens,
            pricing_ref=self.pricing_ref,
            license=self.license,
            owner=self.owner,
        )


class ModelFileConfig(BaseModel):
    """Top level of ``models.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    models: list[ModelConfig] = Field(min_length=1)
    #: Preset table that ``deployment.params.preset`` names, relative to this file.
    presets_file: str = "presets.yaml"

    def to_model_specs(self) -> list[ModelSpec]:
        return [entry.to_model_spec() for entry in self.models]

    def hf_token_expiry(self) -> dict[str, date]:
        """Token ref -> expiry date (``never`` is ``date.max``); the earliest date wins."""
        expiry: dict[str, date] = {}
        for entry in self.models:
            source = entry.source
            if not isinstance(source, HFSourceConfig) or source.token_ref is None:
                continue
            if source.token_expires is None:
                continue
            when = date.max if source.token_expires == "never" else source.token_expires
            expiry[source.token_ref] = min(when, expiry.get(source.token_ref, date.max))
        return expiry


def load_models(path: str | Path, *, base_dir: str | Path | None = None) -> list[ModelSpec]:
    """Parse ``models.yaml`` and return SPI :class:`ModelSpec` entries.

    Entries that name a preset get it expanded into their deployment params; the preset table
    is looked up relative to ``base_dir`` (default: the directory of ``path``). Raises
    :class:`ConfigError` naming the file and the failing field path.
    """
    parsed = _parse(path)
    root = Path(base_dir) if base_dir is not None else Path(path).parent
    return [entry.to_model_spec() for entry in _with_presets(str(path), parsed, root)]


def _with_presets(file: str, parsed: ModelFileConfig, root: Path) -> list[ModelConfig]:
    if not any("preset" in entry.deployment.params for entry in parsed.models):
        return list(parsed.models)
    table_path = root / parsed.presets_file
    table = load_presets(table_path)
    expanded: list[ModelConfig] = []
    for index, entry in enumerate(parsed.models):
        preset_id = entry.deployment.params.get("preset")
        if preset_id is None:
            expanded.append(entry)
            continue
        where = f"models[{index}].deployment.params.preset"
        preset = table.get(str(preset_id))
        if preset is None:
            raise ConfigError(file, where, f"no preset {preset_id!r} in {table_path}")
        source = entry.source
        names = (
            [source.repo, source.mirror_repo or ""]
            if isinstance(source, HFSourceConfig)
            else [source.model_id]
        )
        try:
            params = expand_preset(
                entry.deployment.params,
                preset,
                adapter=entry.deployment.adapter,
                model_ids=names,
            )
        except ValueError as exc:
            raise ConfigError(file, where, str(exc)) from exc
        deployment = entry.deployment.model_copy(update={"params": params})
        expanded.append(entry.model_copy(update={"deployment": deployment}))
    return expanded


def load_hf_token_expiry(path: str | Path) -> dict[str, date]:
    """Parse ``models.yaml`` and return the recorded HF token expiry dates by token ref."""
    return _parse(path).hf_token_expiry()


def _parse(path: str | Path) -> ModelFileConfig:
    file = str(path)
    document = load_yaml_mapping(path)
    try:
        return ModelFileConfig.model_validate(document)
    except Exception as exc:
        raise ConfigError.from_validation(file, exc) from exc
