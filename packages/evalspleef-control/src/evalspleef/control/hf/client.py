"""The injectable Hub client interface plus the huggingface_hub-backed client."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

__all__ = ["HfHubClient", "HubClient", "HubRepoInfo"]


@dataclass(frozen=True)
class HubRepoInfo:
    """The subset of ``HfApi.model_info`` the checker needs."""

    sha: str
    siblings: tuple[str, ...]


@runtime_checkable
class HubClient(Protocol):
    """The ``HfApi``-like surface :class:`HuggingFaceChecker` calls.

    Implementations are synchronous; the checker runs them in a worker
    thread. Tests provide a fake with canned responses.
    """

    def model_info(
        self, repo_id: str, *, revision: str | None = None, token: str | None = None
    ) -> HubRepoInfo: ...

    def file_bytes(
        self, repo_id: str, filename: str, *, revision: str, token: str | None = None
    ) -> bytes: ...

    def can_create_repos(self, *, token: str | None = None) -> bool:
        """Whether ``token`` may create repositories (a read-only token may not)."""
        ...

    def create_private_repo(self, repo_id: str, *, token: str | None = None) -> None: ...

    def copy_revision(
        self,
        src_repo_id: str,
        src_revision: str,
        dst_repo_id: str,
        *,
        token: str | None = None,
    ) -> None: ...


class HfHubClient:
    """Real :class:`HubClient` on top of ``huggingface_hub``.

    Not verified against the real Hub (see the package docstring).
    """

    def __init__(self, *, api: Any | None = None) -> None:
        if api is None:
            from huggingface_hub import HfApi

            api = HfApi()
        self._api = api

    def model_info(
        self, repo_id: str, *, revision: str | None = None, token: str | None = None
    ) -> HubRepoInfo:
        info = self._api.model_info(repo_id, revision=revision, token=token)
        siblings = tuple(s.rfilename for s in (info.siblings or ()))
        return HubRepoInfo(sha=str(info.sha), siblings=siblings)

    def file_bytes(
        self, repo_id: str, filename: str, *, revision: str, token: str | None = None
    ) -> bytes:
        from huggingface_hub import hf_hub_download

        path = hf_hub_download(repo_id, filename, revision=revision, token=token)
        return Path(path).read_bytes()

    def can_create_repos(self, *, token: str | None = None) -> bool:
        """Read the token role from ``whoami``; fine-grained tokens need ``repo.write``."""
        if token is None:
            return False
        access: dict[str, Any] = (
            self._api.whoami(token=token).get("auth", {}).get("accessToken", {})
        )
        role = access.get("role")
        if role in ("write", "admin"):
            return True
        if role != "fineGrained":
            return False
        fine: dict[str, Any] = access.get("fineGrained", {})
        granted: list[str] = list(fine.get("global", []))
        for entry in fine.get("scoped", []):
            granted.extend(entry.get("permissions", []))
        return "repo.write" in granted

    def create_private_repo(self, repo_id: str, *, token: str | None = None) -> None:
        self._api.create_repo(repo_id, private=True, exist_ok=True, token=token)

    def copy_revision(
        self,
        src_repo_id: str,
        src_revision: str,
        dst_repo_id: str,
        *,
        token: str | None = None,
    ) -> None:
        """Mirror one pinned revision: download it, upload it, nothing else."""
        import tempfile

        from huggingface_hub import snapshot_download

        with tempfile.TemporaryDirectory() as tmp:
            snapshot_download(src_repo_id, revision=src_revision, local_dir=tmp, token=token)
            self._api.upload_folder(folder_path=tmp, repo_id=dst_repo_id, token=token)
