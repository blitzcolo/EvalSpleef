from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from evalspleef_model_beam import launcher
from evalspleef_model_beam.errors import LaunchFailed
from evalspleef_model_beam.provider import parse_launch_result, run_subprocess

REQUEST: dict[str, Any] = {
    "name": "spleef-qwen-v1",
    "image": "lmsysorg/sglang@sha256:" + "c" * 64,
    "entrypoint": ["python3", "-m", "sglang.launch_server", "--port", "8000"],
    "ports": [8000],
    "cpu": 8,
    "memory": "32Gi",
    "gpu": "RTX5090",
    "gpu_count": 1,
    "env": {"HF_TOKEN": "hf_x"},
    "secrets": [],
    "keep_warm_seconds": 900,
    "authorized": False,
}

# A stand-in for the ``beam`` package: records what the launcher asked for in the file named by
# RECORD, then answers like ``Pod.deploy`` (or exits like the SDK does on a fatal error).
FAKE_SDK = """
import json, os

RECORD = {record!r}


class Image:
    @staticmethod
    def from_registry(uri):
        return {{"registry": uri}}


class Pod:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def deploy(self, name):
        with open(RECORD, "w") as handle:
            json.dump({{"kwargs": self.kwargs, "name": name, "cwd": os.getcwd(),
                       "token": os.environ.get("BEAM_TOKEN")}}, handle)
        if {exit_code!r} is not None:
            raise SystemExit({exit_code!r})
        print("=> Deployed")
        return {{"deployment_id": "dep-1", "stub_id": "stub-1",
                 "invoke_url": "https://dep-1-8000.app.beam.cloud", "version": 3}}, True
"""


def _fake_sdk(tmp_path: Path, *, exit_code: int | None = None) -> tuple[Path, Path]:
    package = tmp_path / "sdk" / "beam"
    package.mkdir(parents=True)
    record = tmp_path / "record.json"
    (package / "__init__.py").write_text(FAKE_SDK.format(record=str(record), exit_code=exit_code))
    return package.parent, record


def _same_directory(seen: str, expected: Path) -> bool:
    return Path(seen).resolve() == expected.resolve()


def _argv(sdk_root: Path) -> list[str]:
    script = (
        f"import sys, runpy; sys.path.insert(0, {str(sdk_root)!r}); "
        f"runpy.run_path({launcher.__file__!r}, run_name='__main__')"
    )
    return [sys.executable, "-I", "-c", script]


async def test_the_launcher_process_deploys_from_stdin_with_an_authorized_url(
    tmp_path: Path,
) -> None:
    sdk_root, record = _fake_sdk(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    code, stdout, _stderr = await run_subprocess(
        _argv(sdk_root),
        json.dumps(REQUEST).encode(),
        {"BEAM_TOKEN": "tok"},
        str(scratch),
        60.0,
    )
    assert code == 0
    assert parse_launch_result(stdout) == {
        "ok": True,
        "deployment_id": "dep-1",
        "stub_id": "stub-1",
        "invoke_url": "https://dep-1-8000.app.beam.cloud",
        "version": 3,
        "warning": "",
        "error": "",
    }
    seen = json.loads(record.read_text())
    assert seen["kwargs"]["authorized"] is True  # the request said False
    assert seen["kwargs"]["image"] == {"registry": REQUEST["image"]}
    assert seen["kwargs"]["entrypoint"] == REQUEST["entrypoint"]
    assert seen["kwargs"]["env"] == {"HF_TOKEN": "hf_x"}
    assert seen["kwargs"]["secrets"] is None
    assert (seen["name"], seen["token"]) == ("spleef-qwen-v1", "tok")
    assert _same_directory(seen["cwd"], scratch)


async def test_an_sdk_exit_becomes_a_failed_result(tmp_path: Path) -> None:
    sdk_root, _record = _fake_sdk(tmp_path, exit_code=1)
    code, stdout, _stderr = await run_subprocess(
        _argv(sdk_root), json.dumps(REQUEST).encode(), {}, str(tmp_path), 60.0
    )
    result = parse_launch_result(stdout)
    assert code == 1
    assert result is not None and result["ok"] is False
    assert result["error"].startswith("SystemExit")


async def test_a_launcher_past_its_deadline_is_killed(tmp_path: Path) -> None:
    argv = [sys.executable, "-I", "-c", "import threading; threading.Event().wait()"]
    with pytest.raises(LaunchFailed) as caught:
        await run_subprocess(argv, b"", {}, str(tmp_path), 0.5)
    assert caught.value.detail["reason"] == "launch_timeout"
