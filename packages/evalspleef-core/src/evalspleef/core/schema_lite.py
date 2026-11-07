"""evalspleef.core.schema_lite — 无第三方依赖的极简 JSON Schema 校验器。

环境约束：未安装 jsonschema / pytest。本模块只实现本仓库 schemas/*.json 实际
用到的 draft 2020-12 子集，用于 lint examples 与 schema 的一致性：

支持：type（字符串或数组）、properties、required、additionalProperties、
enum、const、items、minItems/maxItems/uniqueItems、contains、minimum/maximum、
exclusiveMinimum/exclusiveMaximum、minLength/maxLength、pattern、anyOf/oneOf/
allOf、if/then/else、not、$ref（本地 #/$defs/... 与同目录跨文件引用）、
format=date-time（基础形状校验）。未知关键字忽略而不是报错。

不保证与完整 jsonschema 实现等价；遇到未覆盖关键字时 CLI 会打印提示。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = [
    "SchemaStore",
    "SchemaValidationError",
    "load_json",
    "validate",
    "validate_file",
]

_DATE_TIME_PATTERN = re.compile(
    r"^(?:[0-9]{4})-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9](?:\.[0-9]+)?Z$"
)

_TYPE_CHECKS = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "null": lambda value: value is None,
}


class SchemaValidationError(ValueError):
    """schema 文件本身无法加载/解析。"""


def load_json(path: str | Path) -> Any:
    """读取 JSON 文件；解析失败抛 SchemaValidationError。"""
    file_path = Path(path)
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(f"{file_path}: JSON 解析失败: {exc}") from exc


def _json_equal(left: Any, right: Any) -> bool:
    """JSON 语义相等（bool 与 int 不相等）。"""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right, strict=False)
        )
    return bool(left == right)


class SchemaStore:
    """加载一个目录下的全部 *.schema.json，并解析相对 $ref。"""

    def __init__(self, schema_dir: str | Path) -> None:
        self.schema_dir = Path(schema_dir)
        self._schemas: dict[str, dict[str, Any]] = {}
        if self.schema_dir.is_dir():
            for path in sorted(self.schema_dir.glob("*.schema.json")):
                document = load_json(path)
                if not isinstance(document, dict):
                    raise SchemaValidationError(f"{path}: schema 顶层必须是对象")
                self._schemas[path.name] = document

    @property
    def names(self) -> tuple[str, ...]:
        """已加载的 schema 文件名（排序）。"""
        return tuple(sorted(self._schemas))

    def get(self, name: str) -> dict[str, Any]:
        """按文件名取 schema；不存在抛 KeyError。"""
        return self._schemas[name]

    def has(self, name: str) -> bool:
        """文件是否存在。"""
        return name in self._schemas

    def resolve(self, ref: str, base_name: str) -> tuple[dict[str, Any], dict[str, Any], str]:
        """解析 $ref，返回 (根文档, 目标子 schema, 目标文件名)。"""
        if "#" in ref:
            file_part, fragment = ref.split("#", 1)
        else:
            file_part, fragment = ref, ""
        target_name = file_part if file_part else base_name
        if target_name not in self._schemas:
            raise SchemaValidationError(f"无法解析 $ref: {ref}（缺 {target_name}）")
        root = self._schemas[target_name]
        target: Any = root
        if fragment and fragment != "/":
            pointer = fragment.lstrip("/")
            for token in pointer.split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                if not isinstance(target, dict) or token not in target:
                    raise SchemaValidationError(f"$ref 片段不存在: {ref}")
                target = target[token]
        if not isinstance(target, dict):
            raise SchemaValidationError(f"$ref 目标不是对象 schema: {ref}")
        return root, target, target_name


def _format_path(path: str) -> str:
    """把路径渲染成便于阅读的 $ 表达式。"""
    return path or "$"


def _validate(
    instance: Any,
    schema: Any,
    *,
    store: SchemaStore,
    base_name: str,
    path: str,
    errors: list[str],
    depth: int = 0,
) -> None:
    """递归校验（内部函数）。"""
    if depth > 64:
        errors.append(f"{_format_path(path)}: $ref 嵌套过深")
        return
    if schema is True:
        return
    if schema is False:
        errors.append(f"{_format_path(path)}: schema 为 false，禁止任何值")
        return
    if not isinstance(schema, dict):
        errors.append(f"{_format_path(path)}: 非法 schema 节点 {type(schema).__name__}")
        return

    if "$ref" in schema:
        _root, target, target_name = store.resolve(str(schema["$ref"]), base_name)
        _validate(
            instance,
            target,
            store=store,
            base_name=target_name,
            path=path,
            errors=errors,
            depth=depth + 1,
        )
        # $ref 同级关键字（draft 2020-12 允许）继续校验。
        remaining = {key: value for key, value in schema.items() if key != "$ref"}
        if remaining:
            _validate(
                instance,
                remaining,
                store=store,
                base_name=base_name,
                path=path,
                errors=errors,
                depth=depth + 1,
            )
        return

    if "type" in schema:
        expected = schema["type"]
        types = [expected] if isinstance(expected, str) else list(expected)
        if not any(_TYPE_CHECKS.get(kind, lambda _value: True)(instance) for kind in types):
            errors.append(f"{_format_path(path)}: 类型应为 {types}，实际 {type(instance).__name__}")
            return

    if "const" in schema and not _json_equal(instance, schema["const"]):
        errors.append(f"{_format_path(path)}: 应等于常量 {schema['const']!r}")
    if "enum" in schema and not any(
        _json_equal(instance, candidate) for candidate in schema["enum"]
    ):
        errors.append(f"{_format_path(path)}: 不在枚举 {schema['enum']} 中，实际 {instance!r}")

    if isinstance(instance, dict):
        required = schema.get("required", ())
        for key in required:
            if key not in instance:
                errors.append(f"{_format_path(path)}: 缺少必填字段 {key!r}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", None)
        for key, value in instance.items():
            child_path = f"{path}.{key}" if path else key
            if key in properties:
                _validate(
                    value,
                    properties[key],
                    store=store,
                    base_name=base_name,
                    path=child_path,
                    errors=errors,
                    depth=depth + 1,
                )
            elif additional is False:
                errors.append(f"{_format_path(child_path)}: 不允许的额外字段")
            elif isinstance(additional, dict):
                _validate(
                    value,
                    additional,
                    store=store,
                    base_name=base_name,
                    path=child_path,
                    errors=errors,
                    depth=depth + 1,
                )

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{_format_path(path)}: 至少 {schema['minItems']} 项")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{_format_path(path)}: 最多 {schema['maxItems']} 项")
        if schema.get("uniqueItems"):
            for index, item in enumerate(instance):
                for other in instance[index + 1 :]:
                    if _json_equal(item, other):
                        errors.append(f"{_format_path(path)}: 存在重复项 {item!r}")
                        break
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(instance):
                _validate(
                    item,
                    items,
                    store=store,
                    base_name=base_name,
                    path=f"{path}[{index}]",
                    errors=errors,
                    depth=depth + 1,
                )
        if "contains" in schema and not any(
            not _collect_errors(
                item,
                schema["contains"],
                store=store,
                base_name=base_name,
            )
            for item in instance
        ):
            errors.append(f"{_format_path(path)}: 没有元素满足 contains")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{_format_path(path)}: 字符串长度小于 {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{_format_path(path)}: 字符串长度大于 {schema['maxLength']}")
        if "pattern" in schema and re.search(schema["pattern"], instance) is None:
            errors.append(f"{_format_path(path)}: 不匹配正则 {schema['pattern']!r}")
        if schema.get("format") == "date-time" and _DATE_TIME_PATTERN.match(instance) is None:
            errors.append(f"{_format_path(path)}: 不是 ISO-8601 UTC 时间")
    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{_format_path(path)}: 小于 minimum={schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{_format_path(path)}: 大于 maximum={schema['maximum']}")
        if "exclusiveMinimum" in schema and instance <= schema["exclusiveMinimum"]:
            errors.append(f"{_format_path(path)}: 不大于 exclusiveMinimum")
        if "exclusiveMaximum" in schema and instance >= schema["exclusiveMaximum"]:
            errors.append(f"{_format_path(path)}: 不小于 exclusiveMaximum")

    for sub_schema in schema.get("allOf", ()):
        _validate(
            instance,
            sub_schema,
            store=store,
            base_name=base_name,
            path=path,
            errors=errors,
            depth=depth + 1,
        )
    if "anyOf" in schema and not any(
        not _collect_errors(instance, sub, store=store, base_name=base_name)
        for sub in schema["anyOf"]
    ):
        errors.append(f"{_format_path(path)}: 不满足 anyOf 任一分支")
    if "oneOf" in schema:
        matched = sum(
            1
            for sub in schema["oneOf"]
            if not _collect_errors(instance, sub, store=store, base_name=base_name)
        )
        if matched != 1:
            errors.append(f"{_format_path(path)}: oneOf 命中 {matched} 个分支（要求恰好 1 个）")
    if "not" in schema and not _collect_errors(
        instance, schema["not"], store=store, base_name=base_name
    ):
        errors.append(f"{_format_path(path)}: 命中了 not 禁止的 schema")
    if "if" in schema:
        if not _collect_errors(instance, schema["if"], store=store, base_name=base_name):
            if "then" in schema:
                _validate(
                    instance,
                    schema["then"],
                    store=store,
                    base_name=base_name,
                    path=path,
                    errors=errors,
                    depth=depth + 1,
                )
        elif "else" in schema:
            _validate(
                instance,
                schema["else"],
                store=store,
                base_name=base_name,
                path=path,
                errors=errors,
                depth=depth + 1,
            )


def _collect_errors(
    instance: Any,
    schema: Any,
    *,
    store: SchemaStore,
    base_name: str,
) -> list[str]:
    """返回子 schema 的错误列表（不修改外部 errors）。"""
    errors: list[str] = []
    _validate(
        instance,
        schema,
        store=store,
        base_name=base_name,
        path="",
        errors=errors,
    )
    return errors


def validate(
    instance: Any,
    schema: Mapping[str, Any],
    *,
    store: SchemaStore | None = None,
    schema_name: str = "",
) -> list[str]:
    """校验实例；返回错误消息列表（空列表表示通过）。"""
    active_store = store if store is not None else SchemaStore(".")
    errors: list[str] = []
    _validate(
        instance,
        dict(schema),
        store=active_store,
        base_name=schema_name,
        path="",
        errors=errors,
    )
    return errors


def validate_file(
    instance_path: str | Path,
    schema_name: str,
    store: SchemaStore,
) -> list[str]:
    """加载 JSON 实例并按指定 schema 文件校验。"""
    instance = load_json(instance_path)
    return validate(instance, store.get(schema_name), store=store, schema_name=schema_name)
