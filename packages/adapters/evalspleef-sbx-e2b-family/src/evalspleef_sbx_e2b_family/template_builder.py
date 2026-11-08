# mypy: disable-error-code=import-untyped
"""Template Builder: turn a task environment's OCI image into a vendor template
(TECH-DESIGN section 7.6).

A task environment's Dockerfile is the single source of truth for Linux tasks.
This module builds the E2B-family vendor template from the image **by digest**
(the image reference must pin ``@sha256:...`` so the build is reproducible),
applies the platform hardening steps — run as the non-root ``agent`` user and
remove sudo (invariant I6) — fixes the CPU/memory sizing on the template, and
records the ``oci digest -> template id`` mapping in a JSON registry file so the
batch freeze can reference the exact template built from a digest.

The registry file format is::

    {
      "templates": {
        "sha256:<digest>": {
          "novita-sandbox": {"template_id": "...", "built_at": 1730900000.0},
          "e2b-selfhost": {"template_id": "...", "built_at": 1730900001.0}
        }
      }
    }
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

VENDOR_NOVITA = "novita"
VENDOR_E2B = "e2b"

#: User commands inside trial sandboxes run as this (non-root) user (I6).
DEFAULT_RUN_AS_USER = "agent"

#: The task workspace every template provides, owned by the run-as user.
WORKSPACE = "/workspace"

#: Best-effort sudo removal across the distro families our task images use.
REMOVE_SUDO_CMD = (
    "if command -v sudo >/dev/null 2>&1; then "
    "(apt-get remove -y sudo 2>/dev/null || apk del sudo 2>/dev/null || "
    "dnf remove -y sudo 2>/dev/null || yum remove -y sudo 2>/dev/null || true); fi"
)

_DIGEST_PREFIX = "sha256:"


@dataclass(frozen=True)
class TemplateBuild:
    """Outcome of one template build."""

    vendor: str
    image: str
    digest: str
    template_id: str
    registry_path: Path


def _template_class(vendor: Literal["novita", "e2b"]) -> Any:
    """Return the vendor SDK ``Template`` class (seam for tests)."""
    if vendor == VENDOR_NOVITA:
        from novita_sandbox import Template

        return Template
    if vendor == VENDOR_E2B:
        from e2b import Template

        return Template
    raise ValueError(f"unknown template vendor: {vendor!r}")


def image_digest(image: str) -> str:
    """Extract and validate the pinned digest of an OCI image reference."""
    _, separator, digest = image.rpartition("@")
    if not separator or not digest.startswith(_DIGEST_PREFIX) or len(digest) <= len(_DIGEST_PREFIX):
        raise ValueError(
            f"image reference must pin a digest as '<name>@sha256:<hex>', got {image!r}"
        )
    return digest


class TemplateRegistry:
    """JSON file mapping OCI digests to the per-provider template ids built
    from them (the ``template_registry`` of TECH-DESIGN section 7.6)."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"templates": {}}
        with self._path.open("r", encoding="utf-8") as handle:
            data: dict[str, Any] = json.load(handle)
        data.setdefault("templates", {})
        return data

    def record(self, digest: str, vendor: str, provider: str, template_id: str) -> None:
        data = self.load()
        per_digest = data["templates"].setdefault(digest, {})
        per_digest[provider] = {
            "template_id": template_id,
            "vendor": vendor,
            "built_at": time.time(),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def lookup(self, digest: str, provider: str) -> str | None:
        entry = self.load()["templates"].get(digest, {})
        record = entry.get(provider)
        return str(record["template_id"]) if record else None


class TemplateBuilder:
    """Build E2B-family vendor templates from pinned OCI images.

    ``client_kwargs`` carries the vendor SDK connection options (``api_key`` /
    ``domain``); the API key is a control-plane credential and is only ever
    passed to the vendor build API, never into a template or sandbox (FR-ISO-02).
    """

    def __init__(
        self,
        registry: TemplateRegistry,
        *,
        vendor: Literal["novita", "e2b"],
        provider: str,
        client_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self._registry = registry
        self._vendor = vendor
        self._provider = provider
        self._client_kwargs = dict(client_kwargs or {})

    def build_from_oci_image(
        self,
        image: str,
        *,
        name: str,
        cpus: int,
        memory_mb: int,
        run_as_user: str = DEFAULT_RUN_AS_USER,
        registry_credentials: Mapping[str, str] | None = None,
    ) -> TemplateBuild:
        """Build a provider template from ``image@sha256:...`` and register it.

        The build pins the template resources (``cpu_count``/``memory_mb``), so
        the capability ceiling of TECH-DESIGN section 7.4.1 holds for every
        sandbox spawned from the template.
        """
        digest = image_digest(image)
        credentials = dict(registry_credentials or {})
        template_cls = _template_class(self._vendor)

        builder = template_cls().from_image(
            image,
            username=credentials.get("username"),
            password=credentials.get("password"),
        )
        # Hardening (I6): the agent runs as a non-root user without sudo. The
        # base provisioning runs as root, so sudo removal must come first and
        # the user switch afterwards.
        from_image_builder = builder.run_cmd(REMOVE_SUDO_CMD, user="root")
        # Task packs mount their starter files at /workspace, owned by the agent; a live
        # Novita run of a template built without it failed every case that used it.
        hardened = from_image_builder.set_user(run_as_user).run_cmd(
            f"mkdir -p {WORKSPACE} && chown {run_as_user}:{run_as_user} {WORKSPACE}", user="root"
        )

        build_info = template_cls.build(
            hardened,
            name,
            cpu_count=cpus,
            memory_mb=memory_mb,
            **self._client_kwargs,
        )
        template_id = str(build_info.template_id)
        self._registry.record(digest, self._vendor, self._provider, template_id)
        return TemplateBuild(
            vendor=self._vendor,
            image=image,
            digest=digest,
            template_id=template_id,
            registry_path=self._registry.path,
        )


__all__ = [
    "DEFAULT_RUN_AS_USER",
    "REMOVE_SUDO_CMD",
    "VENDOR_E2B",
    "VENDOR_NOVITA",
    "TemplateBuild",
    "TemplateBuilder",
    "TemplateRegistry",
    "image_digest",
]
