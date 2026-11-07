"""Hugging Face access checks as an injectable interface.

The control plane implements it with ``huggingface_hub``; model adapters receive it by
constructor injection so they never import the HF client themselves.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping

from .model import HFSource, ValidationReport


class HFAccessChecker(ABC):
    @abstractmethod
    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        """Resolve a branch, tag or sha to a 40-hex commit sha using the deployment identity."""

    @abstractmethod
    async def preflight(self, source: HFSource) -> ValidationReport:
        """Readable repo, accepted licence for gated models, safetensors present, architectures set."""

    @abstractmethod
    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        """sha256 of config.json, generation_config.json, tokenizer files and the chat template."""

    @abstractmethod
    async def ensure_mirror(self, source: HFSource) -> str:
        """Copy the pinned revision into the organisation's private mirror repo and return its name."""

    @abstractmethod
    async def verify_mirror(self, source: HFSource) -> bool:
        """True when the mirror still resolves to the recorded sha."""
