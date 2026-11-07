"""``HuggingFaceChecker``: the SPI HFAccessChecker against an injected Hub client."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

from huggingface_hub.errors import GatedRepoError, HfHubHTTPError, RepositoryNotFoundError

from evalspleef.control.secrets import EnvResolver
from evalspleef.spi.errors import SpleefError
from evalspleef.spi.hf import HFAccessChecker
from evalspleef.spi.model import HFSource, ValidationReport
from evalspleef.spi.secrets import SecretResolver

from .client import HfHubClient, HubClient, HubRepoInfo

__all__ = ["SMALL_FILES", "HFAccessError", "HuggingFaceChecker", "default_mirror_name"]

#: Small files whose hashes are recorded as reproducibility fields (FR-MOD-02).
SMALL_FILES = (
    "config.json",
    "generation_config.json",
    "tokenizer_config.json",
    "tokenizer.json",
    "chat_template.jinja",
)

_REVISION_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class HFAccessError(SpleefError):
    """A Hub operation failed in a way that is not one of the preflight checks."""

    error_class = "provider"
    disposition = "manual"


def default_mirror_name(org: str, source: HFSource) -> str:
    """``mirror-<repo-name>-<sha7>`` under our organisation (TECH-DESIGN 6.5)."""
    repo_name = source.repo.split("/", 1)[1]
    return f"{org}/mirror-{repo_name}-{source.revision[:7]}"


class HuggingFaceChecker(HFAccessChecker):
    """Registration-time HF access checks (FR-MOD-02).

    The Hub client is injected; production uses :class:`HfHubClient`, tests a
    fake with canned responses. HF tokens are resolved from ``token_ref``
    through the secret resolver at the moment of use and never logged.
    """

    def __init__(
        self,
        *,
        client: HubClient | None = None,
        secrets: SecretResolver | None = None,
        org: str = "evalspleef-mirrors",
    ) -> None:
        self._client = client if client is not None else HfHubClient()
        self._secrets = secrets if secrets is not None else EnvResolver()
        self._org = org

    # -- helpers ---------------------------------------------------------------

    def _token(self, token_ref: str | None) -> str | None:
        if token_ref is None:
            return None
        return self._secrets.resolve(token_ref)

    @staticmethod
    async def _run(fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Call the synchronous client off the event loop."""
        return await asyncio.to_thread(fn, *args, **kwargs)

    # -- SPI surface -----------------------------------------------------------

    async def resolve_revision(self, repo: str, revision: str, token_ref: str | None) -> str:
        """Resolve a branch, tag or sha to a 40-hex commit sha (FR-MOD-02)."""
        info: HubRepoInfo = await self._run(
            self._client.model_info, repo, revision=revision, token=self._token(token_ref)
        )
        if not _REVISION_SHA_RE.match(info.sha):
            raise HFAccessError(
                f"repo {repo!r} revision {revision!r} resolved to a non-sha value "
                f"({info.sha[:12]}...); refusing to register without a pinned commit"
            )
        return info.sha

    async def preflight(self, source: HFSource) -> ValidationReport:
        """The four FR-MOD-02 checks; the report details name the failing one."""
        token = self._token(source.token_ref)
        checks: list[tuple[str, bool, str]] = []

        info: Any = None
        repo_readable = True
        detail = "model_info succeeded"
        try:
            info = await self._run(
                self._client.model_info, source.repo, revision=source.revision, token=token
            )
        except GatedRepoError:
            repo_readable = False
            detail = "repository is gated and this identity has not accepted access"
        except RepositoryNotFoundError:
            repo_readable = False
            detail = (
                "repository not found or not readable with this identity "
                "(private repository without access, or wrong repo id)"
            )
        checks.append(("repo_readable", repo_readable, detail))
        if not repo_readable:
            return ValidationReport(ok=False, checks=tuple(checks))

        siblings = tuple(info.siblings)
        config_bytes: bytes | None = None
        license_ok = True
        license_detail = "config.json is downloadable with this identity"
        try:
            config_bytes = await self._run(
                self._client.file_bytes,
                source.repo,
                "config.json",
                revision=source.revision,
                token=token,
            )
        except (GatedRepoError, PermissionError):
            license_ok = False
            license_detail = (
                "gated licence has not been accepted by this HF identity; "
                "accept it on the repository page before registering"
            )
        except RepositoryNotFoundError:
            license_ok = False
            license_detail = "config.json is not downloadable: repository unreadable"
        except OSError as exc:
            license_ok = False
            license_detail = f"config.json download failed: {exc}"
        checks.append(("gated_license_accepted", license_ok, license_detail))

        has_safetensors = any(name.endswith(".safetensors") for name in siblings)
        has_bin = any(name.endswith(".bin") for name in siblings)
        if has_safetensors:
            weights_detail = "safetensors weights present"
        elif has_bin:
            weights_detail = (
                "only pytorch .bin weights found; safetensors is required "
                "(Novita Dedicated cannot load .bin)"
            )
        else:
            weights_detail = "no weight files (*.safetensors or *.bin) found in the repo"
        checks.append(("safetensors_present", has_safetensors, weights_detail))

        architectures_ok = False
        architectures_detail = "config.json was not downloaded"
        if config_bytes is not None:
            try:
                config = json.loads(config_bytes.decode("utf-8"))
                architectures = config.get("architectures")
                architectures_ok = isinstance(architectures, list) and len(architectures) > 0
                architectures_detail = (
                    f"architectures: {architectures}"
                    if architectures_ok
                    else "config.json has no non-empty 'architectures' field"
                )
            except (ValueError, UnicodeDecodeError) as exc:
                architectures_detail = f"config.json is not valid JSON: {exc}"
        checks.append(("architectures_present", architectures_ok, architectures_detail))

        return ValidationReport(ok=all(passed for _, passed, _ in checks), checks=tuple(checks))

    async def small_file_hashes(self, source: HFSource) -> Mapping[str, str]:
        """sha256 of the small reproducibility files present in the repo."""
        token = self._token(source.token_ref)
        info = await self._run(
            self._client.model_info, source.repo, revision=source.revision, token=token
        )
        siblings = set(info.siblings)
        hashes: dict[str, str] = {}
        for name in SMALL_FILES:
            if name not in siblings:
                continue
            content = await self._run(
                self._client.file_bytes,
                source.repo,
                name,
                revision=source.revision,
                token=token,
            )
            hashes[name] = hashlib.sha256(content).hexdigest()
        return hashes

    async def ensure_mirror(self, source: HFSource) -> str:
        """Create the frozen private mirror of the pinned revision; return its name."""
        token = self._token(source.token_ref)
        mirror = source.mirror_repo or default_mirror_name(self._org, source)
        await self._run(self._client.create_private_repo, mirror, token=token)
        await self._run(
            self._client.copy_revision,
            source.repo,
            source.revision,
            mirror,
            token=token,
        )
        return mirror

    async def verify_mirror(self, source: HFSource) -> bool:
        """True while the mirror still resolves to the recorded commit sha."""
        if source.mirror_repo is None:
            return False
        try:
            info: HubRepoInfo = await self._run(
                self._client.model_info,
                source.mirror_repo,
                revision=source.revision,
                token=self._token(source.token_ref),
            )
        except (OSError, HfHubHTTPError, SpleefError):
            return False
        return info.sha == source.revision
