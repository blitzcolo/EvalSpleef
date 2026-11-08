"""``spleef presets``: list, search, show, add, copy, edit, verify and remove launch presets.

Every change is validated against the preset schema before the file is written, so the table
on disk always loads. Exit codes follow the CLI: 0 success, 1 a missing or duplicate preset,
2 a configuration or usage error.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import typer
import yaml

from evalspleef.control.config import (
    ConfigError,
    PresetConfig,
    PresetFileConfig,
    load_presets,
    preset_digest,
    save_presets,
)

from .errors import EXIT_DOMAIN, EXIT_USAGE

presets_app = typer.Typer(no_args_is_help=True, help="Model x hardware launch presets.")

_PRESETS_OPT = typer.Option(Path("config/presets.yaml"), "--presets", help="presets.yaml path")
_JSON_OPT = typer.Option(False, "--json", help="print machine-readable JSON")
_FILE_ARG = typer.Argument(..., help="YAML file holding one preset mapping")
_SET_OPT = typer.Option(
    [], "--set", help="dotted.key=value; the value is parsed as YAML (lists, numbers)"
)
_UNSET_OPT = typer.Option([], "--unset", help="dotted.key to remove")


def _fail(message: str, code: int) -> typer.Exit:
    typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    return typer.Exit(code)


def _load(path: Path) -> PresetFileConfig:
    try:
        return load_presets(path)
    except ConfigError as exc:
        raise _fail(str(exc), EXIT_USAGE) from None


def _store(path: Path, presets: list[PresetConfig]) -> None:
    try:
        table = PresetFileConfig.model_validate(
            {"presets": [preset.model_dump(mode="json") for preset in presets]}
        )
    except ValueError as exc:
        raise _fail(str(exc), EXIT_USAGE) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    save_presets(path, table)


def _replace(table: PresetFileConfig, updated: PresetConfig) -> list[PresetConfig]:
    return [updated if preset.id == updated.id else preset for preset in table.presets]


def _require(table: PresetFileConfig, preset_id: str) -> PresetConfig:
    preset = table.get(preset_id)
    if preset is None:
        raise _fail(f"no preset {preset_id!r}", EXIT_DOMAIN)
    return preset


def _validated(document: dict[str, Any]) -> PresetConfig:
    try:
        return PresetConfig.model_validate(document)
    except ValueError as exc:
        raise _fail(str(exc), EXIT_USAGE) from None


def _row(preset: PresetConfig) -> list[str]:
    hardware = preset.hardware
    vram = f"{hardware.vram_gb_per_gpu:g}GB" if hardware.vram_gb_per_gpu else "-"
    last = preset.verified[-1] if preset.verified else None
    return [
        preset.id,
        preset.model,
        f"{hardware.provider}:{hardware.gpu}x{hardware.gpu_count}",
        vram,
        preset.engine,
        str(preset.context_length or "-"),
        f"{last.result} {last.date}" if last else "unverified",
    ]


def _emit(presets: list[PresetConfig], as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps([preset.model_dump(mode="json") for preset in presets], indent=2))
        return
    headers = ["id", "model", "hardware", "vram/gpu", "engine", "context", "verified"]
    rows = [_row(preset) for preset in presets]
    widths = [
        max(len(str(cell)) for cell in column) for column in zip(headers, *rows, strict=False)
    ]
    for line in [headers, ["-" * width for width in widths], *rows]:
        typer.echo("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(line)).rstrip())


@presets_app.command("list")
def list_presets(
    presets: Path = _PRESETS_OPT,
    model: str | None = typer.Option(None, "--model", help="substring of the model name"),
    gpu: str | None = typer.Option(None, "--gpu", help="GPU type, e.g. RTX5090"),
    gpus: int | None = typer.Option(None, "--gpus", help="GPU count"),
    as_json: bool = _JSON_OPT,
) -> None:
    """List presets, optionally filtered by model, GPU type and GPU count."""
    _emit(_load(presets).search(model=model, gpu=gpu, gpu_count=gpus), as_json)


@presets_app.command("search")
def search_presets(
    text: str = typer.Argument(..., help="text matched against every field"),
    presets: Path = _PRESETS_OPT,
    as_json: bool = _JSON_OPT,
) -> None:
    """Find presets whose id, model, arguments, facts or notes contain TEXT."""
    _emit(_load(presets).search(text), as_json)


@presets_app.command("show")
def show_preset(
    preset_id: str = typer.Argument(...), presets: Path = _PRESETS_OPT, as_json: bool = _JSON_OPT
) -> None:
    """Print one preset in full, with the digest models.yaml entries record."""
    preset = _require(_load(presets), preset_id)
    document = {
        **preset.model_dump(mode="json", exclude_none=True),
        "digest": preset_digest(preset),
    }
    if as_json:
        typer.echo(json.dumps(document, indent=2))
    else:
        typer.echo(yaml.safe_dump(document, sort_keys=False, allow_unicode=True).rstrip())


@presets_app.command("add")
def add_preset(
    file: Path = _FILE_ARG,
    presets: Path = _PRESETS_OPT,
) -> None:
    """Add the preset in FILE; its id must be new."""
    try:
        document = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise _fail(f"{file}: {exc}", EXIT_USAGE) from None
    if not isinstance(document, dict):
        raise _fail(f"{file}: expected one preset mapping", EXIT_USAGE)
    preset = _validated(document)
    table = _load(presets)
    if table.get(preset.id) is not None:
        raise _fail(f"preset {preset.id!r} already exists; use edit", EXIT_DOMAIN)
    _store(presets, [*table.presets, preset])
    typer.echo(f"added {preset.id}")


@presets_app.command("copy")
def copy_preset(
    preset_id: str = typer.Argument(...),
    new_id: str = typer.Argument(...),
    presets: Path = _PRESETS_OPT,
) -> None:
    """Copy a preset under a new id, without its verification log."""
    table = _load(presets)
    source = _require(table, preset_id)
    if table.get(new_id) is not None:
        raise _fail(f"preset {new_id!r} already exists", EXIT_DOMAIN)
    document = source.model_dump(mode="json")
    document.update({"id": new_id, "verified": []})
    _store(presets, [*table.presets, _validated(document)])
    typer.echo(f"copied {preset_id} to {new_id}")


def _set_path(document: dict[str, Any], dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    node = document
    for key in keys[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    if value is _UNSET:
        node.pop(keys[-1], None)
    else:
        node[keys[-1]] = value


_UNSET = object()


@presets_app.command("edit")
def edit_preset(
    preset_id: str = typer.Argument(...),
    set_: list[str] = _SET_OPT,
    unset: list[str] = _UNSET_OPT,
    presets: Path = _PRESETS_OPT,
) -> None:
    """Change fields of a preset, e.g. --set hardware.vram_gb_per_gpu=32 --set notes='...'."""
    if not set_ and not unset:
        raise _fail("nothing to change: pass --set or --unset", EXIT_USAGE)
    table = _load(presets)
    document = _require(table, preset_id).model_dump(mode="json")
    for assignment in set_:
        key, sep, raw = assignment.partition("=")
        if not sep or not key:
            raise _fail(f"--set needs key=value, got {assignment!r}", EXIT_USAGE)
        if key == "id":
            raise _fail("the id cannot change; copy the preset instead", EXIT_USAGE)
        try:
            value = yaml.safe_load(raw) if raw else ""
        except yaml.YAMLError as exc:
            raise _fail(f"--set {key}: {exc}", EXIT_USAGE) from None
        _set_path(document, key, value)
    for key in unset:
        _set_path(document, key, _UNSET)
    _store(presets, _replace(table, _validated(document)))
    typer.echo(f"updated {preset_id}")


@presets_app.command("verify")
def verify_preset(
    preset_id: str = typer.Argument(...),
    result: str = typer.Option(..., "--result", help="ok, failed or partial"),
    summary: str = typer.Option(..., "--summary"),
    evidence: str | None = typer.Option(None, "--evidence", help="log path, commit or URL"),
    when: str | None = typer.Option(None, "--date", help="YYYY-MM-DD, default today"),
    presets: Path = _PRESETS_OPT,
) -> None:
    """Append a verification record to a preset."""
    table = _load(presets)
    document = _require(table, preset_id).model_dump(mode="json")
    record = {"date": when or date.today().isoformat(), "result": result, "summary": summary}
    if evidence:
        record["evidence"] = evidence
    document["verified"] = [*document.get("verified", []), record]
    _store(presets, _replace(table, _validated(document)))
    typer.echo(f"recorded {result} for {preset_id}")


@presets_app.command("remove")
def remove_preset(preset_id: str = typer.Argument(...), presets: Path = _PRESETS_OPT) -> None:
    """Delete a preset. Specs already registered from it keep its id and digest."""
    table = _load(presets)
    _require(table, preset_id)
    _store(presets, [preset for preset in table.presets if preset.id != preset_id])
    typer.echo(f"removed {preset_id}")
