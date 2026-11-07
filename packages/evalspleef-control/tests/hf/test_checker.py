"""Tests for HuggingFaceChecker with a canned fake Hub client."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import httpx2
import pytest
from huggingface_hub.utils import GatedRepoError, RepositoryNotFoundError

from evalspleef.control.hf import HuggingFaceChecker, default_mirror_name
from evalspleef.control.hf.checker import HFAccessError
from evalspleef.control.hf.client import HubRepoInfo
from evalspleef.spi.model import HFSource
from evalspleef.testing.secrets import DictSecretResolver

SHA = "3f9c2b7d" * 5  # 40 hex chars
OTHER_SHA = "8b1e4a2c" * 5
TOKEN_REF = "vault://secret/hf/eval"


def _gated_error() -> GatedRepoError:
    response = httpx2.Response(403, request=httpx2.Request("GET", "http://hf/api"))
    return GatedRepoError("gated repo", response=response)


def _not_found_error() -> RepositoryNotFoundError:
    response = httpx2.Response(404, request=httpx2.Request("GET", "http://hf/api"))
    return RepositoryNotFoundError("repo not found", response=response)


@dataclass
class FakeRepo:
    """Canned repository: a sha, a file set, and optional failure modes."""

    sha: str = SHA
    files: dict[str, bytes] = field(default_factory=dict)
    gated: bool = False
    missing: bool = False


class FakeHubClient:
    """Implements the HubClient protocol with canned repositories."""

    def __init__(self, repos: dict[str, FakeRepo]) -> None:
        self.repos = repos
        self.created: list[str] = []
        self.copied: list[tuple[str, str, str]] = []
        self.tokens_seen: list[str | None] = []

    def model_info(
        self, repo_id: str, *, revision: str | None = None, token: str | None = None
    ) -> HubRepoInfo:
        self.tokens_seen.append(token)
        repo = self.repos[repo_id]
        if repo.missing:
            raise _not_found_error()
        if repo.gated:
            raise _gated_error()
        return HubRepoInfo(sha=repo.sha, siblings=tuple(repo.files))

    def file_bytes(
        self, repo_id: str, filename: str, *, revision: str, token: str | None = None
    ) -> bytes:
        repo = self.repos[repo_id]
        if repo.gated:
            raise _gated_error()
        try:
            return repo.files[filename]
        except KeyError:
            raise FileNotFoundError(filename) from None

    def create_private_repo(self, repo_id: str, *, token: str | None = None) -> None:
        self.created.append(repo_id)

    def copy_revision(
        self,
        src_repo_id: str,
        src_revision: str,
        dst_repo_id: str,
        *,
        token: str | None = None,
    ) -> None:
        self.copied.append((src_repo_id, src_revision, dst_repo_id))


def _config_json(architectures: list[str] | None = None) -> bytes:
    value = ["Qwen3ForCausalLM"] if architectures is None else architectures
    return json.dumps({"architectures": value}).encode()


def _public_repo() -> FakeRepo:
    return FakeRepo(
        files={
            "config.json": _config_json(),
            "generation_config.json": b"{}",
            "model.safetensors": b"weights",
            "tokenizer.json": b"tok",
        }
    )


def _source(**overrides: object) -> HFSource:
    base: dict[str, object] = {
        "repo": "our-org/model",
        "revision": SHA,
        "access": "public",
        "token_ref": TOKEN_REF,
    }
    base.update(overrides)
    return HFSource(**base)  # type: ignore[arg-type]


def _checker(client: FakeHubClient) -> HuggingFaceChecker:
    secrets = DictSecretResolver({TOKEN_REF: "hf-token-123"})
    return HuggingFaceChecker(client=client, secrets=secrets)


async def test_resolve_revision_from_branch() -> None:
    client = FakeHubClient({"org/model": _public_repo()})
    checker = _checker(client)
    sha = await checker.resolve_revision("org/model", "main", TOKEN_REF)
    assert sha == SHA
    assert client.tokens_seen == ["hf-token-123"]


async def test_resolve_revision_rejects_non_sha() -> None:
    client = FakeHubClient({"org/model": FakeRepo(sha="refs/heads/main")})
    checker = _checker(client)
    with pytest.raises(HFAccessError, match="non-sha"):
        await checker.resolve_revision("org/model", "main", None)


async def test_preflight_public_repo_passes() -> None:
    client = FakeHubClient({"our-org/model": _public_repo()})
    report = await _checker(client).preflight(_source())
    assert report.ok
    assert [check for check, passed, _ in report.checks if passed] == [
        "repo_readable",
        "gated_license_accepted",
        "safetensors_present",
        "architectures_present",
    ]


async def test_preflight_gated_not_accepted_has_distinct_reason() -> None:
    client = FakeHubClient({"our-org/model": FakeRepo(gated=True)})
    report = await _checker(client).preflight(_source(access="gated"))
    assert not report.ok
    failures = {check: detail for check, passed, detail in report.checks if not passed}
    assert "repo_readable" in failures
    assert "gated" in failures["repo_readable"]
    assert "not accepted" in failures["repo_readable"]


async def test_preflight_private_no_access_has_distinct_reason() -> None:
    client = FakeHubClient({"our-org/model": FakeRepo(missing=True)})
    report = await _checker(client).preflight(_source(access="private"))
    assert not report.ok
    failures = {check: detail for check, passed, detail in report.checks if not passed}
    assert "repo_readable" in failures
    assert "private" in failures["repo_readable"]
    assert len(report.checks) == 1  # further checks are skipped


async def test_preflight_bin_only_repo_rejected() -> None:
    repo = FakeRepo(files={"config.json": _config_json(), "pytorch_model.bin": b"old"})
    client = FakeHubClient({"our-org/model": repo})
    report = await _checker(client).preflight(_source())
    assert not report.ok
    failures = {check: detail for check, passed, detail in report.checks if not passed}
    assert "safetensors_present" in failures
    assert ".bin" in failures["safetensors_present"]


async def test_preflight_missing_architectures_rejected() -> None:
    repo = FakeRepo(files={"config.json": _config_json([]), "model.safetensors": b"w"})
    client = FakeHubClient({"our-org/model": repo})
    report = await _checker(client).preflight(_source())
    assert not report.ok
    failures = {check: detail for check, passed, detail in report.checks if not passed}
    assert "architectures_present" in failures


async def test_preflight_invalid_config_json_rejected() -> None:
    repo = FakeRepo(files={"config.json": b"not json", "model.safetensors": b"w"})
    client = FakeHubClient({"our-org/model": repo})
    report = await _checker(client).preflight(_source())
    failures = {check: detail for check, passed, detail in report.checks if not passed}
    assert "architectures_present" in failures
    assert "JSON" in failures["architectures_present"]


async def test_small_file_hashes_only_present_files() -> None:
    repo = FakeRepo(
        files={
            "config.json": _config_json(),
            "model.safetensors": b"weights",
        }
    )
    client = FakeHubClient({"our-org/model": repo})
    hashes = await _checker(client).small_file_hashes(_source())
    assert hashes == {"config.json": hashlib.sha256(_config_json()).hexdigest()}


async def test_ensure_mirror_creates_private_repo_and_copies() -> None:
    client = FakeHubClient({"our-org/model": _public_repo()})
    checker = _checker(client)
    mirror = await checker.ensure_mirror(_source())
    assert mirror == f"evalspleef-mirrors/mirror-model-{SHA[:7]}"
    assert client.created == [mirror]
    assert client.copied == [("our-org/model", SHA, mirror)]

    # an explicit mirror_repo wins over the generated name
    named = await checker.ensure_mirror(_source(mirror_repo="evalspleef-mirrors/custom"))
    assert named == "evalspleef-mirrors/custom"


def test_default_mirror_name() -> None:
    assert default_mirror_name("org", _source()) == f"org/mirror-model-{SHA[:7]}"


async def test_verify_mirror_compares_sha() -> None:
    client = FakeHubClient(
        {
            "our-org/model": _public_repo(),
            "evalspleef-mirrors/mirror-model-ok": FakeRepo(sha=SHA),
            "evalspleef-mirrors/mirror-model-stale": FakeRepo(sha=OTHER_SHA),
        }
    )
    checker = _checker(client)
    assert await checker.verify_mirror(_source(mirror_repo="evalspleef-mirrors/mirror-model-ok"))
    assert not await checker.verify_mirror(
        _source(mirror_repo="evalspleef-mirrors/mirror-model-stale")
    )
    assert not await checker.verify_mirror(_source())  # no mirror recorded


async def test_token_values_never_surface_in_reports() -> None:
    client = FakeHubClient({"our-org/model": FakeRepo(missing=True)})
    report = await _checker(client).preflight(_source())
    rendered = repr(report.checks)
    assert "hf-token-123" not in rendered
