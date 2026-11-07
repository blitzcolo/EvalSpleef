"""Test fixtures and fake doubles for evalspleef-sbx-device-pool tests."""

from __future__ import annotations

import stat
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from evalspleef.spi.secrets import SecretRef


class DummySecrets:
    def resolve(self, ref: SecretRef | str) -> str:
        raise RuntimeError(f"no secrets resolved: {ref}")


@pytest.fixture
def fake_hdc_dir() -> Generator[Path, None, None]:
    with tempfile.TemporaryDirectory(prefix="fake_hdc_test_") as tmp:
        p = Path(tmp)
        script = p / "hdc"
        script.write_text(
            """#!/usr/bin/env python3
import sys

args = sys.argv[1:]
if len(args) >= 2 and args[0] == "-t":
    args = args[2:]

if not args:
    print("hdc mock")
    sys.exit(0)

cmd = args[0]
if cmd == "list":
    print("Hate-AL00-001\\nHate-AL00-002")
elif cmd == "install":
    print("Success")
elif cmd == "uninstall":
    print("Success")
elif cmd == "shell":
    sub = args[1:]
    if sub and sub[0] == "bm" and len(sub) >= 2 and sub[1] == "dump":
        print("com.huawei.system\\ncom.ohos.settings")
    elif sub and sub[0] == "reboot":
        print("Rebooting")
    elif sub and sub[0] == "hilog":
        print("mock hilog entry")
    else:
        print("shell ok")
sys.exit(0)
""",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        yield p
