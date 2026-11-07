"""The fake SDK mimics the real SDK signatures used by the adapter.

Each entry pins the exact parameter names of the corresponding real SDK
method (imported from the installed ``novita-sandbox`` / ``e2b`` packages) and
checks the fake against them, so an SDK upgrade that renames a parameter the
adapter relies on fails here instead of silently misbinding at runtime.

For instance methods the fake must cover the full parameter list of the real
method in order (a prefix is accepted only for trailing vendor extensions such
as e2b's ``retain_output``). For class-level ``Sandbox.create`` the fake
declares the subset the adapter passes, and every declared name must exist in
the real signature. ``Sandbox.kill`` is dual-dispatch in the real SDKs
(instance method and class-level ``kill(sandbox_id)``); the class-level shape
is introspected through ``typing.get_overloads``.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from typing import Any

import pytest

# Real e2b surfaces.
from e2b.sandbox_sync.commands.command import Commands as E2BCommands
from e2b.sandbox_sync.filesystem.filesystem import Filesystem as E2BFilesystem
from e2b.sandbox_sync.main import Sandbox as E2BSandbox
from evalspleef_sbx_e2b_family.fakes import FakeE2BCluster, make_fake_sandbox_class
from evalspleef_sbx_e2b_family.fakes.core import FakeCommands, FakeFilesystem, FakeSandbox

# Real novita-sandbox 2.x surfaces (sync API, which the adapter drives).
from novita_sandbox.core.sandbox_sync.commands.command import Commands as NovitaCommands
from novita_sandbox.core.sandbox_sync.filesystem.filesystem import (
    Filesystem as NovitaFilesystem,
)
from novita_sandbox.core.sandbox_sync.main import Sandbox as NovitaSandbox

_RUN = [
    "cmd",
    "background",
    "envs",
    "user",
    "cwd",
    "on_stdout",
    "on_stderr",
    "stdin",
    "timeout",
    "request_timeout",
]
_FS_WRITE = ["path", "data", "user", "request_timeout"]
_FS_READ = ["path", "format", "user", "request_timeout"]

# (real callable, fake callable, expected parameter names in order)
METHOD_CASES: list[tuple[Callable[..., Any], Callable[..., Any], list[str]]] = [
    (NovitaCommands.run, FakeCommands.run, _RUN),
    (NovitaCommands.kill, FakeCommands.kill, ["pid", "request_timeout"]),
    (NovitaCommands.send_stdin, FakeCommands.send_stdin, ["pid", "data", "request_timeout"]),
    (NovitaFilesystem.write, FakeFilesystem.write, _FS_WRITE),
    (NovitaFilesystem.read, FakeFilesystem.read, _FS_READ),
    (E2BCommands.run, FakeCommands.run, _RUN),
    (E2BCommands.kill, FakeCommands.kill, ["pid", "request_timeout"]),
    (E2BCommands.send_stdin, FakeCommands.send_stdin, ["pid", "data", "request_timeout"]),
    (E2BFilesystem.write, FakeFilesystem.write, _FS_WRITE),
    (E2BFilesystem.read, FakeFilesystem.read, _FS_READ),
    (NovitaSandbox.set_network, FakeSandbox.set_network, ["allow_out", "deny_out", "opts"]),
    (E2BSandbox.update_network, FakeSandbox.update_network, ["network", "opts"]),
    (E2BSandbox.create_snapshot, FakeSandbox.create_snapshot, ["name", "mode", "opts"]),
]

CREATE_PARAMS = ["template", "timeout", "metadata", "envs", "allow_internet_access", "network"]
LIST_PARAMS = ["query", "limit", "next_token"]


def _names(func: Callable[..., Any]) -> list[str]:
    return list(inspect.signature(func).parameters)


@pytest.mark.parametrize(
    "real,fake,expected",
    METHOD_CASES,
    ids=[f"{case[0].__qualname__}" for case in METHOD_CASES],
)
def test_fake_method_signatures_match_real_sdk(
    real: Callable[..., Any], fake: Callable[..., Any], expected: list[str]
) -> None:
    real_params = [name for name in _names(real) if name != "self"]
    fake_params = [name for name in _names(fake) if name != "self"]
    # every fake parameter must exist in the real signature, in the same order
    assert all(name in real_params for name in fake_params)
    assert fake_params == expected


@pytest.mark.parametrize("real_create", [NovitaSandbox.create, E2BSandbox.create])
def test_fake_sandbox_create_signature(real_create: Callable[..., Any]) -> None:
    real_params = [name for name in _names(real_create) if name != "self"]
    fake_params = _names(make_fake_sandbox_class(FakeE2BCluster()).create)
    for name in CREATE_PARAMS:
        assert name in real_params, f"real SDK create lost parameter {name!r}"
    assert fake_params == [*CREATE_PARAMS, "opts"]


@pytest.mark.parametrize("real_kill", [NovitaSandbox.kill, E2BSandbox.kill])
def test_fake_sandbox_kill_signature(real_kill: Callable[..., Any]) -> None:
    overloads = typing.get_overloads(real_kill)
    assert any("sandbox_id" in _names(overload) for overload in overloads), (
        "real SDK kill has no class-level sandbox_id overload"
    )
    fake_params = _names(make_fake_sandbox_class(FakeE2BCluster()).kill)
    assert fake_params == ["sandbox_id", "opts"]


@pytest.mark.parametrize("real_list", [NovitaSandbox.list, E2BSandbox.list])
def test_fake_sandbox_list_signature(real_list: Callable[..., Any]) -> None:
    real_params = [name for name in _names(real_list) if name != "self"]
    for name in LIST_PARAMS:
        assert name in real_params, f"real SDK list lost parameter {name!r}"
    fake_params = _names(make_fake_sandbox_class(FakeE2BCluster()).list)
    assert fake_params == [*LIST_PARAMS, "opts"]


def test_novita_create_snapshot_takes_no_positional_arguments() -> None:
    """The novita adapter calls ``create_snapshot()`` bare; the fake must accept that call."""
    assert _names(NovitaSandbox.create_snapshot) == ["self", "opts"]
    inspect.signature(FakeSandbox.create_snapshot).bind(None)
