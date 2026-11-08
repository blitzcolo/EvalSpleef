from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from evalspleef.control.config import ConfigError, load_models, load_presets, preset_digest

REPO = "orcarouter/Qwen3.8-27B-Uncensored-NVFP4"
SHA = "d350e02d82203d5bf07a1e203c997581f235f407"
PRESET_ID = "qwen3.8-27b-uncensored-nvfp4/beam-rtx5090x1/sglang"


def _preset(**overrides: Any) -> dict[str, Any]:
    preset: dict[str, Any] = {
        "id": PRESET_ID,
        "model": REPO,
        "adapter": "beam-pod",
        "hardware": {"provider": "beam", "gpu": "RTX5090", "gpu_count": 1, "vram_gb_per_gpu": 32},
        "engine": "sglang",
        "engine_args": ["--mem-fraction-static", "0.88", "--chunked-prefill-size", "2048"],
        "context_length": 32768,
        "max_concurrency": 2,
        "deployment_params": {"cpu": 8, "memory": "48Gi", "weights_volume": "huggingface_models"},
        "facts": {"weights_gib": 23.02, "precision": "NVFP4 + FP8"},
        "notes": "measured on Beam",
    }
    preset.update(overrides)
    return preset


def _write(tmp: Path, presets: list[dict[str, Any]], params: dict[str, Any], **model: Any) -> Path:
    (tmp / "presets.yaml").write_text(yaml.safe_dump({"presets": presets}), encoding="utf-8")
    entry = {
        "slug": "qwen38-27b",
        "family": "qwen3",
        "roles": ["target"],
        "source": {"type": "huggingface", "repo": REPO, "revision": SHA, "access": "gated"},
        "deployment": {"adapter": "beam-pod", "account": "beam", "params": params},
        "declared_context_tokens": 32768,
        "declared_max_output_tokens": 4096,
        "pricing_ref": "beam",
        "license": "apache-2.0",
        "owner": "eval",
        **model,
    }
    path = tmp / "models.yaml"
    path.write_text(yaml.safe_dump({"models": [entry]}), encoding="utf-8")
    return path


def test_a_preset_fills_the_deployment_params_and_is_recorded(tmp_path: Path) -> None:
    path = _write(tmp_path, [_preset()], {"preset": PRESET_ID, "cpu": 16})
    (spec,) = load_models(path)
    params = spec.deployment.params
    assert params["gpu"] == "RTX5090" and params["gpu_count"] == 1
    assert params["engine"] == "sglang"
    assert params["engine_extra_args"] == [
        "--mem-fraction-static",
        "0.88",
        "--chunked-prefill-size",
        "2048",
    ]
    assert (params["context_length"], params["max_concurrency"]) == (32768, 2)
    assert params["weights_volume"] == "huggingface_models"
    assert params["cpu"] == 16  # the explicit value wins
    assert params["preset"] == PRESET_ID
    table = load_presets(tmp_path / "presets.yaml")
    preset = table.get(PRESET_ID)
    assert preset is not None and params["preset_digest"] == preset_digest(preset)


def test_the_digest_follows_the_launch_shape_not_the_notes(tmp_path: Path) -> None:
    base = load_presets(_table(tmp_path, [_preset()])).presets[0]
    noted = load_presets(_table(tmp_path, [_preset(notes="other", facts={})])).presets[0]
    changed = load_presets(
        _table(tmp_path, [_preset(engine_args=["--mem-fraction-static", "0.80"])])
    ).presets[0]
    assert preset_digest(base) == preset_digest(noted)
    assert preset_digest(base) != preset_digest(changed)


def _table(tmp: Path, presets: list[dict[str, Any]]) -> Path:
    path = tmp / "table.yaml"
    path.write_text(yaml.safe_dump({"presets": presets}), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("presets", "message"),
    [
        ([], "no preset"),
        ([_preset(model="other/model")], "is for 'other/model'"),
        ([_preset(adapter="novita-serverless-gpu")], "targets adapter"),
    ],
)
def test_a_preset_that_does_not_belong_to_the_entry_is_refused(
    tmp_path: Path, presets: list[dict[str, Any]], message: str
) -> None:
    path = _write(tmp_path, presets, {"preset": PRESET_ID})
    with pytest.raises(ConfigError) as caught:
        load_models(path)
    assert caught.value.path == "models[0].deployment.params.preset"
    assert message in str(caught.value)


@pytest.mark.parametrize(
    "presets",
    [
        [_preset(), _preset()],
        [_preset(id="Bad Id")],
        [_preset(hardware={"provider": "beam", "gpu": "RTX5090", "gpu_count": 9})],
        [_preset(deployment_params={"gpu_count": 2})],
    ],
)
def test_invalid_tables_name_the_problem(tmp_path: Path, presets: list[dict[str, Any]]) -> None:
    with pytest.raises(ConfigError):
        load_presets(_table(tmp_path, presets))


def test_models_without_presets_do_not_need_a_table(tmp_path: Path) -> None:
    path = _write(tmp_path, [], {"gpu": "RTX5090"})
    (tmp_path / "presets.yaml").write_text(": not yaml :", encoding="utf-8")
    (spec,) = load_models(path)
    assert "preset" not in spec.deployment.params
