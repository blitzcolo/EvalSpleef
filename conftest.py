"""Send every temporary file of a test session to one scratch directory and remove it at the end.

Test doubles create a directory per fake sandbox and some tests never destroy them. Keep the
prefix short: unix socket paths are limited to about 100 characters.
"""

from __future__ import annotations

import os
import shutil
import tempfile

import pytest

_state: dict[str, str | None] = {}


def pytest_configure(config: pytest.Config) -> None:
    scratch = tempfile.mkdtemp(prefix="sp-")
    _state["scratch"] = scratch
    _state["tempdir"] = tempfile.tempdir
    _state["TMPDIR"] = os.environ.get("TMPDIR")
    tempfile.tempdir = scratch
    os.environ["TMPDIR"] = scratch


def pytest_unconfigure(config: pytest.Config) -> None:
    scratch = _state.pop("scratch", None)
    tempfile.tempdir = _state.pop("tempdir", None)
    previous = _state.pop("TMPDIR", None)
    if previous is None:
        os.environ.pop("TMPDIR", None)
    else:
        os.environ["TMPDIR"] = previous
    if scratch is not None:
        shutil.rmtree(scratch, ignore_errors=True)
