"""Tests for the builtin mock conformance targets."""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest

from evalspleef.conformance.builtin import build_targets
from evalspleef.conformance.target import ConformanceTarget
from evalspleef.spi.caps import NetMode
from evalspleef.testing.fake_openai import FakeOpenAIServer
from evalspleef.testing.mock_endpoint import (
    MockApiProvider,
    MockAssistedProvider,
    MockExternalProvider,
)


def test_build_targets_returns_four_fake_targets() -> None:
    targets = build_targets()
    assert len(targets) == 4
    assert all(target.fake for target in targets)
    names = {target.name for target in targets}
    assert names == {
        "mock-sandbox",
        "mock-model-api",
        "mock-model-assisted",
        "mock-model-external",
    }


def test_target_kinds_and_providers() -> None:
    by_name = {target.name: target for target in build_targets()}
    sandbox = by_name["mock-sandbox"]
    assert sandbox.kind == "sandbox"
    assert sandbox.spec_factory is not None
    for name in ("mock-model-api", "mock-model-assisted", "mock-model-external"):
        target = by_name[name]
        assert target.kind == "model"
        assert target.model_fixture is not None
        assert target.model_fixture.spec_factory is not None


def test_assisted_target_has_operator_input_factory() -> None:
    by_name = {target.name: target for target in build_targets()}
    assisted = by_name["mock-model-assisted"]
    assert isinstance(assisted.provider, MockAssistedProvider)
    assert assisted.model_fixture is not None
    factory = assisted.model_fixture.operator_input_factory
    assert factory is not None
    operator_input = factory()
    assert operator_input["model"] == "fake-model"
    assert operator_input["base_url"].startswith("https://")
    for name in ("mock-model-api", "mock-model-external"):
        target = by_name[name]
        assert target.model_fixture is not None
        assert target.model_fixture.operator_input_factory is None
    assert isinstance(by_name["mock-model-api"].provider, MockApiProvider)
    assert isinstance(by_name["mock-model-external"].provider, MockExternalProvider)


def test_build_targets_starts_no_upstream_and_no_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    starts: list[FakeOpenAIServer] = []
    real_start = FakeOpenAIServer.start

    def recording_start(self: FakeOpenAIServer) -> None:
        starts.append(self)
        real_start(self)

    listens: list[socket.socket] = []
    real_listen = socket.socket.listen

    def recording_listen(self: socket.socket, *args: object, **kwargs: object) -> None:
        listens.append(self)
        real_listen(self, *args, **kwargs)

    monkeypatch.setattr(FakeOpenAIServer, "start", recording_start)
    monkeypatch.setattr(socket.socket, "listen", recording_listen)

    baseline = {thread.ident for thread in threading.enumerate()}
    targets = build_targets()
    new_nondaemon = [
        thread.name
        for thread in threading.enumerate()
        if thread.ident not in baseline and thread.is_alive() and not thread.daemon
    ]

    assert starts == [], "building targets must not start an upstream server"
    assert listens == [], "building targets must not open a listening socket"
    assert new_nondaemon == [], f"building targets leaked threads: {new_nondaemon}"

    async def cleanup_all() -> None:
        for target in targets:
            if target.cleanup is not None:
                await target.cleanup()

    asyncio.run(cleanup_all())


async def test_mock_sandbox_target_opens_and_closes() -> None:
    (sandbox_target,) = [target for target in build_targets() if target.name == "mock-sandbox"]
    assert isinstance(sandbox_target, ConformanceTarget)
    provider = sandbox_target.provider
    async with sandbox_target.open_sandbox(network=NetMode.OFF) as sandbox:
        result = await sandbox.exec(["echo", "ok"])
        assert result.exit_code == 0
        listed = await provider.list({"conformance": "builtin"})
        assert any(entry["id"] == sandbox.id for entry in listed)
    assert sandbox.destroyed
    listed_after = await provider.list({"conformance": "builtin"})
    assert all(entry["id"] != sandbox.id for entry in listed_after)


async def test_sandbox_spec_factory_uses_arguments() -> None:
    (sandbox_target,) = [target for target in build_targets() if target.name == "mock-sandbox"]
    assert sandbox_target.spec_factory is not None
    spec = sandbox_target.spec_factory("trial-x", network=NetMode.DOCS_WHITELIST, max_lifetime_s=42)
    assert spec.trial_id == "trial-x"
    assert spec.network.mode is NetMode.DOCS_WHITELIST
    assert spec.max_lifetime_s == 42
