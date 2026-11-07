"""Conversion between SPI ``ModelSpec`` and ``ModelSpecRow`` documents."""

from __future__ import annotations

from typing import Any

from evalspleef.control.store.rows import ModelSpecRow
from evalspleef.spi.model import (
    DeploymentSpec,
    HFSource,
    ModelSpec,
    VendorSource,
)

__all__ = ["model_spec_from_row", "model_spec_to_row"]


def source_to_dict(source: HFSource | VendorSource) -> dict[str, Any]:
    """Serialize an SPI model source to its JSON document form."""
    if isinstance(source, HFSource):
        return {
            "type": "huggingface",
            "repo": source.repo,
            "revision": source.revision,
            "access": source.access,
            "token_ref": source.token_ref,
            "mirror_repo": source.mirror_repo,
            "adapter_repo": source.adapter_repo,
            "files_sha256": dict(source.files_sha256),
        }
    return {"type": "vendor", "vendor": source.vendor, "model_id": source.model_id}


def source_from_dict(document: dict[str, Any]) -> HFSource | VendorSource:
    """Rebuild an SPI model source from its JSON document form."""
    kind = document.get("type")
    if kind == "huggingface":
        return HFSource(
            repo=str(document["repo"]),
            revision=str(document["revision"]),
            access=document["access"],
            token_ref=document.get("token_ref"),
            mirror_repo=document.get("mirror_repo"),
            adapter_repo=document.get("adapter_repo"),
            files_sha256=dict(document.get("files_sha256", {})),
        )
    if kind == "vendor":
        return VendorSource(vendor=str(document["vendor"]), model_id=str(document["model_id"]))
    raise ValueError(f"unknown model source type in stored document: {kind!r}")


def model_spec_to_row(spec: ModelSpec, *, created_at: str) -> ModelSpecRow:
    """Project an immutable SPI spec into a store row (new ``spec_version``)."""
    deployment: dict[str, Any] = {
        "adapter": spec.deployment.adapter,
        "account": spec.deployment.account,
        "params": dict(spec.deployment.params),
    }
    return ModelSpecRow(
        slug=spec.slug,
        spec_version=spec.spec_version,
        family=spec.family,
        roles=tuple(sorted(spec.roles)),
        source=source_to_dict(spec.source),
        deployment=deployment,
        declared_context_tokens=spec.declared_context_tokens,
        declared_max_output_tokens=spec.declared_max_output_tokens,
        pricing_ref=spec.pricing_ref,
        license=spec.license,
        owner=spec.owner,
        created_at=created_at,
    )


def model_spec_from_row(row: ModelSpecRow) -> ModelSpec:
    """Rebuild the SPI spec from a store row."""
    return ModelSpec(
        slug=row.slug,
        spec_version=row.spec_version,
        family=row.family,
        roles=frozenset(row.roles),  # type: ignore[arg-type]
        source=source_from_dict(row.source),
        deployment=DeploymentSpec(
            adapter=str(row.deployment["adapter"]),
            account=str(row.deployment["account"]),
            params=dict(row.deployment.get("params", {})),
        ),
        declared_context_tokens=row.declared_context_tokens,
        declared_max_output_tokens=row.declared_max_output_tokens,
        pricing_ref=row.pricing_ref,
        license=row.license,
        owner=row.owner,
    )
