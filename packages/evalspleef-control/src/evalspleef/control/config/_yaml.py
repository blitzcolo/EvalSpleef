"""Shared YAML loading helpers for the control-plane configuration files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .errors import ConfigError


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    """Load one YAML document and require a top-level mapping.

    YAML errors and shape mismatches are reported as :class:`ConfigError`
    naming the file; the parsed value is returned unchanged so callers keep
    full control over schema validation.
    """
    file = str(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(file, "", f"cannot read file: {exc.strerror or exc}") from exc
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(file, where, f"invalid YAML: {exc}") from exc
    if document is None:
        raise ConfigError(file, "", "file is empty")
    if not isinstance(document, dict):
        raise ConfigError(file, "", "top-level document must be a mapping")
    return document
