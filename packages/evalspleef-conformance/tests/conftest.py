"""Shared fixtures for the conformance kit tests."""

import pytest

import evalspleef.conformance.plugin as plugin

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _no_installed_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sessions started by these tests must not load whatever adapters are installed."""
    monkeypatch.setattr(plugin, "entry_points", lambda *, group: ())
