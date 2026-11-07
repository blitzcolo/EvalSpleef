"""The model registry: immutable ``ModelSpec`` versions and judge-family rules."""

from __future__ import annotations

import builtins
from collections.abc import Callable

from evalspleef.control.store.repos import ModelSpecRepository
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.model import ModelSpec

from .serialization import model_spec_from_row, model_spec_to_row

__all__ = ["ModelNotFoundError", "ModelRegistry", "SameFamilyJudgeError"]


class ModelNotFoundError(SpleefError):
    """A ``slug@version`` reference does not resolve to a registered spec."""

    error_class = "version"
    disposition = "manual"


class SameFamilyJudgeError(SpleefError):
    """Judge and target model share a family, violating PRD appendix C.5."""

    error_class = "judge"
    disposition = "manual"


def default_now() -> str:
    """ISO-8601 UTC timestamp used as the row's ``created_at``."""
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class ModelRegistry:
    """Registry of immutable model spec versions (FR-MOD-01).

    Specs are stored through :class:`ModelSpecRepository`; a ``(slug,
    spec_version)`` pair is written once and never updated. ``now`` is
    injectable so tests stay deterministic.
    """

    def __init__(
        self,
        specs: ModelSpecRepository,
        *,
        now: Callable[[], str] = default_now,
    ) -> None:
        self._specs = specs
        self._now = now

    async def register(self, spec: ModelSpec) -> None:
        """Register a new immutable spec version.

        Re-registering an existing ``(slug, spec_version)`` raises
        :class:`~evalspleef.control.store.repos.ModelSpecExistsError`: model
        specs are immutable, any change must carry a new ``spec_version``.
        """
        await self._specs.insert(model_spec_to_row(spec, created_at=self._now()))

    async def get(self, slug: str, spec_version: int) -> ModelSpec | None:
        """Fetch one exact spec version, or None when it is not registered."""
        row = await self._specs.get(slug, spec_version)
        return model_spec_from_row(row) if row else None

    async def get_latest(self, slug: str) -> ModelSpec | None:
        """Fetch the highest registered spec version of ``slug``."""
        rows = await self._specs.list_by_slug(slug)
        return model_spec_from_row(rows[-1]) if rows else None

    async def list(self, slug: str) -> builtins.list[ModelSpec]:
        """List all registered spec versions of one slug, oldest first.

        The store's model-spec table is keyed by slug; there is deliberately
        no repository-wide scan, so listing is per slug.
        """
        rows = await self._specs.list_by_slug(slug)
        return [model_spec_from_row(row) for row in rows]

    async def resolve(self, ref: str) -> ModelSpec:
        """Resolve ``slug@version`` (or bare ``slug`` = latest version)."""
        slug, sep, version_text = ref.partition("@")
        if not slug:
            raise ModelNotFoundError(f"empty model reference: {ref!r}")
        if sep:
            try:
                spec_version = int(version_text)
            except ValueError:
                raise ModelNotFoundError(
                    f"model reference {ref!r} has a non-integer version {version_text!r}"
                ) from None
            spec = await self.get(slug, spec_version)
            if spec is None:
                raise ModelNotFoundError(f"no model spec registered for {ref!r}")
            return spec
        spec = await self.get_latest(slug)
        if spec is None:
            raise ModelNotFoundError(f"no model spec registered for slug {slug!r}")
        return spec

    @staticmethod
    def judge_family_check(judge: ModelSpec, target: ModelSpec) -> builtins.list[str]:
        """Reasons ``judge`` may not judge ``target``; empty means allowed (C.5)."""
        if judge.family == target.family:
            return [
                f"judge family {judge.family!r} equals target family "
                f"(slug {judge.slug!r} may not judge {target.slug!r})"
            ]
        return []

    @staticmethod
    def require_distinct_judge_family(judge: ModelSpec, target: ModelSpec) -> None:
        """Enforce the anti-same-source judge rule; raises on violation."""
        reasons = ModelRegistry.judge_family_check(judge, target)
        if reasons:
            raise SameFamilyJudgeError(reasons[0])
