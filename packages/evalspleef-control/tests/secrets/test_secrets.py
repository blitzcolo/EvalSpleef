"""Tests for the control-plane secret resolvers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from evalspleef.control.secrets import (
    CompositeResolver,
    EnvResolver,
    FileResolver,
    SecretResolutionError,
    UnsupportedSchemeError,
    VaultKv2Resolver,
)
from evalspleef.spi.secrets import SecretResolver


def test_env_resolver_reads_variable() -> None:
    resolver = EnvResolver({"NOVITA_API_KEY": "n0vita"})
    assert resolver.resolve("env://NOVITA_API_KEY") == "n0vita"


def test_env_resolver_uses_real_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPLEEF_TEST_TOKEN", "tok-123")
    resolver = EnvResolver()
    assert resolver.resolve("env://SPLEEF_TEST_TOKEN") == "tok-123"


def test_env_resolver_missing_variable_names_variable_only() -> None:
    resolver = EnvResolver({})
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve("env://DOES_NOT_EXIST")
    assert "DOES_NOT_EXIST" in str(excinfo.value)
    assert "environment variable is not set" in str(excinfo.value)


def test_env_resolver_wrong_scheme() -> None:
    resolver = EnvResolver({})
    with pytest.raises(UnsupportedSchemeError):
        resolver.resolve("vault://secret/hf/key")


def test_env_resolver_empty_variable() -> None:
    resolver = EnvResolver({})
    with pytest.raises(SecretResolutionError):
        resolver.resolve("env://")


def test_file_resolver_reads_contents(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("hf_secret_value\n", encoding="utf-8")
    resolver = FileResolver()
    assert resolver.resolve(f"file://{secret}") == "hf_secret_value"


def test_file_resolver_multiline_kept(tmp_path: Path) -> None:
    secret = tmp_path / "pem"
    secret.write_text("line1\nline2\n", encoding="utf-8")
    resolver = FileResolver()
    assert resolver.resolve(f"file://{secret}") == "line1\nline2"


def test_file_resolver_missing_file() -> None:
    resolver = FileResolver()
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve("file:///nonexistent/secret")
    assert "/nonexistent/secret" in str(excinfo.value)


def test_file_resolver_requires_absolute_path(tmp_path: Path) -> None:
    resolver = FileResolver()
    with pytest.raises(SecretResolutionError):
        resolver.resolve("file://relative/path")


def _vault_transport(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_vault_resolver_happy_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/secret/data/hf/eval"
        assert request.headers["X-Vault-Token"] == "vault-token"
        return httpx.Response(
            200, json={"data": {"data": {"readonly": "hf_ro_token", "other": "x"}}}
        )

    resolver = VaultKv2Resolver(
        "http://vault.internal:8200", "vault-token", client=_vault_transport(handler)
    )
    assert resolver.resolve("vault://secret/hf/eval/readonly") == "hf_ro_token"


def test_vault_resolver_deeper_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/kv/data/team/llm/evals/hf"
        return httpx.Response(200, json={"data": {"data": {"key": "v"}}})

    resolver = VaultKv2Resolver(
        "http://vault.internal:8200/", "t", client=_vault_transport(handler)
    )
    assert resolver.resolve("vault://kv/team/llm/evals/hf/key") == "v"


def test_vault_resolver_forbidden_hides_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"errors": ["permission denied"]})

    resolver = VaultKv2Resolver(
        "http://vault.internal:8200", "vault-token", client=_vault_transport(handler)
    )
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve("vault://secret/hf/eval/readonly")
    message = str(excinfo.value)
    assert "denied" in message
    assert "vault-token" not in message
    assert "hf_ro_token" not in message


def test_vault_resolver_missing_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"data": {"other": "x"}}})

    resolver = VaultKv2Resolver("http://vault.internal:8200", "t", client=_vault_transport(handler))
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve("vault://secret/hf/eval/readonly")
    assert "'readonly'" in str(excinfo.value)


def test_vault_resolver_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    resolver = VaultKv2Resolver("http://vault.internal:8200", "t", client=_vault_transport(handler))
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve("vault://secret/hf/eval/readonly")
    assert "no secret" in str(excinfo.value)


def test_vault_resolver_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    resolver = VaultKv2Resolver("http://vault.internal:8200", "t", client=_vault_transport(handler))
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve("vault://secret/hf/eval/readonly")
    assert "unreachable" in str(excinfo.value)


def test_vault_resolver_requires_three_components() -> None:
    resolver = VaultKv2Resolver("http://vault.internal:8200", "t")
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve("vault://secret/hf")
    assert "vault://" in str(excinfo.value)


def test_vault_rejects_non_http_base_url() -> None:
    with pytest.raises(ValueError):
        VaultKv2Resolver("vault.internal:8200", "t")


def test_composite_dispatches_by_scheme(tmp_path: Path) -> None:
    secret = tmp_path / "token"
    secret.write_text("from-file", encoding="utf-8")
    composite = CompositeResolver(
        [
            EnvResolver({"FROM_ENV": "from-env"}),
            FileResolver(),
        ]
    )
    assert composite.resolve("env://FROM_ENV") == "from-env"
    assert composite.resolve(f"file://{secret}") == "from-file"


def test_composite_unknown_scheme() -> None:
    composite = CompositeResolver([EnvResolver({})])
    with pytest.raises(UnsupportedSchemeError) as excinfo:
        composite.resolve("vault://secret/hf/key")
    assert "vault://" in str(excinfo.value)


def test_composite_needs_at_least_one_resolver() -> None:
    with pytest.raises(ValueError):
        CompositeResolver([])


def test_resolvers_satisfy_spi_protocol() -> None:
    def accepts(resolver: SecretResolver) -> str:
        return resolver.resolve("env://X")

    assert accepts(EnvResolver({"X": "ok"})) == "ok"
    assert accepts(CompositeResolver([EnvResolver({"X": "ok"})])) == "ok"


def test_values_never_appear_in_errors() -> None:
    """A resolved value must not leak through a later failure."""
    resolver = EnvResolver({"API_KEY": "super-secret-value"})
    assert resolver.resolve("env://API_KEY") == "super-secret-value"
    with pytest.raises(SecretResolutionError) as excinfo:
        resolver.resolve("env://MISSING")
    assert "super-secret-value" not in str(excinfo.value)
