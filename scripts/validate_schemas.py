#!/usr/bin/env python3
"""Schema 契约校验（可选依赖：PyYAML + jsonschema；仅用于 CI/收尾终验）。

标准库环境（无 PyYAML/jsonschema）下本脚本会打印安装提示并以退出码 2 结束；
仓库的常规自检仍用 scripts/check_repo.py（纯标准库）。

用法：
    python3 scripts/validate_schemas.py
    # 推荐在独立 venv 中跑：
    python3 -m venv /tmp/llmeval-venv
    /tmp/llmeval-venv/bin/pip install pyyaml jsonschema
    /tmp/llmeval-venv/bin/python scripts/validate_schemas.py

校验范围：
1. schemas/*.json 自身是合法的 JSON Schema（draft 2020-12），$id 与文件名一致；
2. appendix/examples/<id>/task.yaml   -> task.schema.json
3. appendix/examples/<id>/rubric.yaml -> rubric.schema.json
4. appendix/examples/<id>/defects.yaml-> defect.schema.json（catalog 文档）
5. reference/examples/*.json          -> 按文件名前缀匹配对应 Schema
退出码：0 全部通过；1 有失败；2 缺依赖。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "schemas"

print(f"[info] Python {sys.version.split()[0]}]".replace("]]", "]"))

try:
    import yaml  # type: ignore
except ImportError:
    print(
        "缺少 PyYAML：请 python3 -m venv /tmp/llmeval-venv && /tmp/llmeval-venv/bin/pip install pyyaml jsonschema"
    )
    raise SystemExit(2) from None

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError:
    print("缺少 jsonschema/referencing：请安装后重跑（见脚本头注释）")
    raise SystemExit(2) from None


def load_schemas() -> dict[str, dict]:
    schemas: dict[str, dict] = {}
    for p in sorted(SCHEMA_DIR.glob("*.schema.json")):
        data = json.loads(p.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(data)
        expected = f"https://llm-eval.local/schemas/{p.name}"
        if data.get("$id") != expected:
            print(f"[ERROR] {p.name}: $id 应为 {expected}，实际 {data.get('$id')}")
        schemas[p.name] = data
    return schemas


def make_validator(name: str, schemas: dict[str, dict]):
    resources = [(schema.get("$id"), Resource.from_contents(schema)) for schema in schemas.values()]
    registry = Registry().with_resources(resources)
    return Draft202012Validator(schemas[name], registry=registry)


def load_yaml(path: Path):
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def report(path: Path, validator, doc) -> bool:
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.path))
    if not errors:
        return True
    print(f"[FAIL] {path.relative_to(ROOT)}（{len(errors)} 处）")
    for err in errors[:5]:
        loc = "/".join(str(p) for p in err.path) or "<root>"
        print(f"        - {loc}: {err.message[:240]}")
    if len(errors) > 5:
        print(f"        ... 另有 {len(errors) - 5} 处")
    return False


def main() -> int:
    schemas = load_schemas()
    print(f"[info] 已加载 {len(schemas)} 个 Schema：{', '.join(sorted(schemas))}")

    validators = {name: make_validator(name, schemas) for name in schemas}
    ok = True
    checked = 0

    # 题包
    for pkg in sorted((ROOT / "appendix" / "examples").iterdir()):
        if not pkg.is_dir():
            continue
        for fname, schema_name in (
            ("task.yaml", "task.schema.json"),
            ("rubric.yaml", "rubric.schema.json"),
            ("defects.yaml", "defect.schema.json"),
        ):
            path = pkg / fname
            if not path.is_file():
                print(f"[SKIP] {pkg.name}/{fname} 不存在")
                ok = False
                continue
            try:
                doc = load_yaml(path)
            except Exception as exc:
                print(f"[FAIL] {path.relative_to(ROOT)}: YAML 解析失败 {exc}")
                ok = False
                continue
            checked += 1
            ok = report(path, validators[schema_name], doc) and ok

    # 参考实现样例
    ref_examples = ROOT / "reference" / "examples"
    if ref_examples.is_dir():
        prefix_map = [
            ("task", "task.schema.json"),
            ("rubric", "rubric.schema.json"),
            ("defect", "defect.schema.json"),
            ("feedback", "feedback.schema.json"),
            ("grade", "grade.schema.json"),
            ("trial", "trial.schema.json"),
        ]
        for path in sorted(ref_examples.glob("*.json")):
            schema_name = next(
                (s for prefix, s in prefix_map if path.name.startswith(prefix)), None
            )
            if not schema_name:
                print(f"[SKIP] {path.name} 未匹配到 Schema（交给人审）")
                continue
            try:
                doc = load_json(path)
            except Exception as exc:
                print(f"[FAIL] {path.relative_to(ROOT)}: JSON 解析失败 {exc}")
                ok = False
                continue
            checked += 1
            ok = report(path, validators[schema_name], doc) and ok

    print(f"\n结果：{'PASS' if ok else 'FAIL'}（共校验 {checked} 个文档）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
