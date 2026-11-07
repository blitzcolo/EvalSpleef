#!/usr/bin/env python3
"""Schema contract validation for task packs and core examples.

Validation covers:
1. every file in schemas/*.json is valid JSON (and a valid draft 2020-12 schema when
   the jsonschema package is installed), with an $id matching its filename;
2. tasks/<id>/task.yaml -> task.schema.json, rubric.yaml -> rubric.schema.json,
   defects.yaml -> defect.schema.json;
3. runtime lint from PRD FR-TSK-03 on every task pack: runtime block present, images
   carry a digest or the TODO(pin-on-host) marker, max_lifetime_s covers the budget,
   and in-sandbox execution declares gateway egress;
4. packages/evalspleef-core/examples/**/*.json -> the schema matching the file prefix.

Runs with the installed environment: PyYAML is required; jsonschema/referencing are
used when importable, otherwise evalspleef.core.schema_lite is the fallback.

Usage:
    python3 scripts/validate_schemas.py
    uv run --frozen --no-sync python scripts/validate_schemas.py

Exit codes: 0 = all documents valid; 1 = at least one failure; 2 = missing dependency.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"
TASKS_DIR = ROOT / "tasks"
CORE_EXAMPLES = ROOT / "packages" / "evalspleef-core" / "examples"
PIN_MARKER = "TODO(pin-on-host)"

PACK_FILES = (
    ("task.yaml", "task.schema.json"),
    ("rubric.yaml", "rubric.schema.json"),
    ("defects.yaml", "defect.schema.json"),
)

EXAMPLE_PREFIXES = (
    ("task", "task.schema.json"),
    ("rubric", "rubric.schema.json"),
    ("defect", "defect.schema.json"),
    ("feedback", "feedback.schema.json"),
    ("grade", "grade.schema.json"),
    ("trial", "trial.schema.json"),
)

WARNINGS: list[str] = []
FAILURES: list[str] = []


def warn(message: str) -> None:
    WARNINGS.append(message)
    print(f"[WARN] {message}")


def fail(message: str) -> None:
    FAILURES.append(message)
    print(f"[ERROR] {message}")


def load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


print(f"[info] Python {sys.version.split()[0]}")

try:
    import yaml  # type: ignore
except ImportError:
    print("missing PyYAML: run inside the workspace environment (uv sync --all-packages)")
    raise SystemExit(2) from None

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource

    HAVE_JSONSCHEMA = True
except ImportError:
    HAVE_JSONSCHEMA = False
    try:
        from evalspleef.core import schema_lite
    except ImportError:
        print(
            "missing jsonschema/referencing and evalspleef.core.schema_lite: "
            "run inside the workspace environment"
        )
        raise SystemExit(2) from None


class Validators:
    """Uniform error reporting over jsonschema or the dependency-free fallback."""

    def __init__(self, schemas: dict[str, dict[str, Any]]) -> None:
        self.schemas = schemas
        if HAVE_JSONSCHEMA:
            resources = [
                (schema.get("$id"), Resource.from_contents(schema)) for schema in schemas.values()
            ]
            registry = Registry().with_resources(resources)
            self._validators = {
                name: Draft202012Validator(schema, registry=registry)
                for name, schema in schemas.items()
            }
        else:
            self._store = schema_lite.SchemaStore(SCHEMA_DIR)

    def errors(self, name: str, document: Any) -> list[tuple[str, str]]:
        if HAVE_JSONSCHEMA:
            found = sorted(self._validators[name].iter_errors(document), key=lambda e: list(e.path))
            return [("/".join(str(p) for p in e.path) or "<root>", e.message) for e in found]
        messages = schema_lite.validate(
            document, self.schemas[name], store=self._store, schema_name=name
        )
        parsed: list[tuple[str, str]] = []
        for message in messages:
            location, _, detail = message.partition(": ")
            parsed.append((location or "<root>", detail or message))
        return parsed


def load_schemas() -> dict[str, dict[str, Any]]:
    schemas: dict[str, dict[str, Any]] = {}
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        data = load_json(path)
        if not isinstance(data, dict):
            fail(f"{path.name}: schema root must be an object")
            continue
        if HAVE_JSONSCHEMA:
            try:
                Draft202012Validator.check_schema(data)
            except Exception as exc:  # jsonschema raises SchemaError
                fail(f"{path.name}: invalid JSON Schema ({exc})")
                continue
        expected = f"https://llm-eval.local/schemas/{path.name}"
        if data.get("$id") != expected:
            fail(f"{path.name}: $id should be {expected}, got {data.get('$id')}")
        schemas[path.name] = data
    return schemas


def report(path: Path, validator: Validators, schema_name: str, document: Any) -> bool:
    errors = validator.errors(schema_name, document)
    if not errors:
        return True
    print(f"[FAIL] {path.relative_to(ROOT)} ({len(errors)} errors)")
    for location, message in errors[:5]:
        print(f"        - {location}: {message[:240]}")
    if len(errors) > 5:
        print(f"        ... {len(errors) - 5} more")
    return False


def lint_task_runtime(path: Path, document: Any) -> bool:
    """Apply the PRD FR-TSK-03 runtime rules that JSON Schema cannot express."""
    ok = True
    rel = path.relative_to(ROOT)
    runtime = document.get("runtime") if isinstance(document, dict) else None
    if not isinstance(runtime, dict):
        fail(f"{rel}: runtime block is required for a task pack")
        return False

    requirements = runtime.get("requirements")
    environment = runtime.get("environment")
    if not isinstance(requirements, dict) or not isinstance(environment, dict):
        return True  # schema validation already reported the shape error

    for field in ("oci_image", "macos_image", "windows_image"):
        value = environment.get(field)
        if not isinstance(value, str):
            continue
        if PIN_MARKER in value:
            warn(f"{rel}: {field} is not pinned yet ({PIN_MARKER})")
        elif "@sha256:" not in value:
            fail(f"{rel}: {field} must be pinned with @sha256:")
            ok = False

    budget = document.get("budget")
    lifetime = requirements.get("max_lifetime_s")
    if isinstance(budget, dict) and isinstance(lifetime, int):
        required = budget.get("max_wall_clock_s", 0) + budget.get("max_rounds", 0) * budget.get(
            "max_observe_s", 0
        )
        if lifetime < required:
            fail(
                f"{rel}: max_lifetime_s={lifetime} is below "
                f"max_wall_clock_s + max_rounds * max_observe_s={required}"
            )
            ok = False

    modes = runtime.get("execution_modes") or []
    if isinstance(modes, list) and "in-sandbox" in modes:
        allow_hosts = requirements.get("allow_hosts") or []
        if not allow_hosts:
            fail(f"{rel}: in-sandbox requires a declared gateway egress in allow_hosts")
            ok = False
    return ok


def validate_packs(validator: Validators) -> tuple[bool, int]:
    ok = True
    checked = 0
    if not TASKS_DIR.is_dir():
        fail("missing tasks/ directory")
        return False, 0
    for pkg in sorted(p for p in TASKS_DIR.iterdir() if p.is_dir()):
        task_document: Any = None
        for filename, schema_name in PACK_FILES:
            path = pkg / filename
            if not path.is_file():
                fail(f"{pkg.name}/{filename} is missing")
                ok = False
                continue
            try:
                document = load_yaml(path)
            except Exception as exc:
                fail(f"{path.relative_to(ROOT)}: YAML parse error {exc}")
                ok = False
                continue
            checked += 1
            ok = report(path, validator, schema_name, document) and ok
            if filename == "task.yaml":
                task_document = document
        if isinstance(task_document, dict):
            ok = lint_task_runtime(pkg / "task.yaml", task_document) and ok
    return ok, checked


def validate_core_examples(validator: Validators) -> tuple[bool, int]:
    ok = True
    checked = 0
    if not CORE_EXAMPLES.is_dir():
        fail("missing packages/evalspleef-core/examples/ directory")
        return False, 0
    for path in sorted(CORE_EXAMPLES.rglob("*.json")):
        schema_name = next(
            (schema for prefix, schema in EXAMPLE_PREFIXES if path.name.startswith(prefix)),
            None,
        )
        if not schema_name:
            print(f"[SKIP] {path.relative_to(ROOT)} does not match a schema (human review)")
            continue
        try:
            document = load_json(path)
        except Exception as exc:
            fail(f"{path.relative_to(ROOT)}: JSON parse error {exc}")
            ok = False
            continue
        checked += 1
        ok = report(path, validator, schema_name, document) and ok
    return ok, checked


def main() -> int:
    schemas = load_schemas()
    mode = "jsonschema" if HAVE_JSONSCHEMA else "evalspleef.core.schema_lite"
    print(f"[info] validator: {mode}; loaded {len(schemas)} schemas: {', '.join(sorted(schemas))}")

    validator = Validators(schemas)
    packs_ok, packs_checked = validate_packs(validator)
    examples_ok, examples_checked = validate_core_examples(validator)

    ok = (
        packs_ok
        and examples_ok
        and not FAILURES
        and len(schemas) == len(list(SCHEMA_DIR.glob("*.schema.json")))
    )
    checked = packs_checked + examples_checked
    print(
        f"\nresult: {'PASS' if ok else 'FAIL'} ({checked} documents checked, "
        f"{len(WARNINGS)} warnings)"
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
