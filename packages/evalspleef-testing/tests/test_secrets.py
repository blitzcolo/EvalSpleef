from __future__ import annotations

import pytest

from evalspleef.spi.secrets import SecretRef
from evalspleef.testing.secrets import DictSecretResolver, EnvSecretResolver


def test_dict_resolver_returns_values_by_reference() -> None:
    resolver = DictSecretResolver({"vault://hf/eval-readonly": "hf-token", "NOVITA_API_KEY": "n1"})

    assert resolver.resolve(SecretRef("vault://hf/eval-readonly")) == "hf-token"
    assert resolver.resolve("NOVITA_API_KEY") == "n1"


def test_dict_resolver_reads_the_mapping_at_resolve_time() -> None:
    values = {"env://NAME": "first"}
    resolver = DictSecretResolver(values)

    assert resolver.resolve("env://NAME") == "first"
    values["env://NAME"] = "second"
    assert resolver.resolve("env://NAME") == "second"


def test_dict_resolver_rejects_unknown_references() -> None:
    with pytest.raises(KeyError, match="missing"):
        DictSecretResolver().resolve("missing")


def test_env_resolver_reads_the_injected_environment() -> None:
    resolver = EnvSecretResolver({"NOVITA_API_KEY": "n1"})

    assert resolver.resolve("env://NOVITA_API_KEY") == "n1"
    assert resolver.resolve(SecretRef("env://NOVITA_API_KEY")) == "n1"


def test_env_resolver_reads_the_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPLEEF_TEST_SECRET", "value")

    assert EnvSecretResolver().resolve("env://SPLEEF_TEST_SECRET") == "value"


def test_env_resolver_rejects_other_schemes() -> None:
    with pytest.raises(ValueError, match="env://"):
        EnvSecretResolver({}).resolve("vault://hf/eval-readonly")


def test_env_resolver_rejects_a_nameless_reference() -> None:
    with pytest.raises(ValueError, match="names no variable"):
        EnvSecretResolver({}).resolve("env://")


def test_env_resolver_rejects_a_missing_variable() -> None:
    with pytest.raises(KeyError, match="MISSING"):
        EnvSecretResolver({}).resolve("env://MISSING")
