"""Fixtures for the observe tests: pack loading and mock sandbox lifecycle."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from evalspleef.control.taskpack import TaskPack, load_task_pack
from evalspleef.testing.clock import FakeClock
from evalspleef.testing.mock_sandbox import MockSandboxProvider
from evalspleef.testing.secrets import DictSecretResolver

_MODULE = "evalspleef_control_tests_observe_harness"


def harness() -> object:
    """Import the shared helper module by path (test dirs are not importable packages)."""
    if _MODULE not in sys.modules:
        path = Path(__file__).resolve().parent / "_harness.py"
        spec = importlib.util.spec_from_file_location(_MODULE, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[_MODULE] = module
        spec.loader.exec_module(module)
    return sys.modules[_MODULE]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(1_788_000_000.0)


@pytest.fixture
def provider(clock: FakeClock) -> MockSandboxProvider:
    return MockSandboxProvider("mock", {}, DictSecretResolver(), clock=clock)


@pytest.fixture
def hmod():
    """The by-path imported helper module (packs, sandbox builders, fixtures)."""
    return harness()


@pytest.fixture
def pack_factory() -> Callable[[str], TaskPack]:
    mod = harness()
    return lambda task_id: load_task_pack(mod.pack_dir(task_id))


@pytest.fixture
def make_trial_ctx(provider: MockSandboxProvider):
    mod = harness()

    async def build(pack: TaskPack, tmp_path: Path, *, stage_agent=None) -> tuple[object, object]:
        return await mod.make_ctx(provider, pack, tmp_path, stage_agent=stage_agent)

    return build


@pytest.fixture(autouse=True)
async def cleanup(provider: MockSandboxProvider) -> AsyncIterator[None]:
    yield
    for sandbox in provider.created_sandboxes():
        await sandbox.destroy()
