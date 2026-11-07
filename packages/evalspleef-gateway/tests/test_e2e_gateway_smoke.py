"""Pytest entry for the real-HTTP gateway smoke (see e2e_gateway_smoke.py)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SMOKE_PATH = Path(__file__).with_name("e2e_gateway_smoke.py")


def _load_smoke_module():
    spec = importlib.util.spec_from_file_location("e2e_gateway_smoke", _SMOKE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_e2e_gateway_smoke() -> None:
    module = _load_smoke_module()
    paths = module.run_smoke()
    assert paths, "expected transcript files to be written"
