"""工具调用的边界检查。

这里只检查模型提交的结构和应用权限。工具实际能做什么由 tools.py 的
固定注册表决定；这不是操作系统级沙箱。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any


MAX_ARGUMENT_BYTES = 16_384
MAX_DEPTH = 8
MAX_NODES = 512
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_]{1,80}$")


class ValidationError(ValueError):
    """参数不符合工具声明时使用的可读错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _walk_limits(value: Any, depth: int = 0, count: list[int] | None = None) -> None:
    """限制 JSON 的深度和节点数，避免异常大的嵌套输入。"""
    if count is None:
        count = [0]
    count[0] += 1
    if count[0] > MAX_NODES:
        raise ValidationError("too_many_values", "参数内容过于复杂。")
    if depth > MAX_DEPTH:
        raise ValidationError("too_deep", "参数嵌套层数过多。")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 120:
                raise ValidationError("invalid_key", "参数字段名无效。")
            _walk_limits(child, depth + 1, count)
    elif isinstance(value, list):
        for child in value:
            _walk_limits(child, depth + 1, count)
    elif isinstance(value, str) and len(value) > 8_000:
        raise ValidationError("string_too_long", "单个文本参数过长。")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValidationError("non_finite_number", "参数不能包含非有限数字。")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """拒绝重复 JSON 字段，避免不同组件对同一参数产生不同理解。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate_json_key", "工具参数包含重复字段。")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    """Python JSON 默认接受 NaN/Infinity，这些值不是标准 JSON 数字。"""
    raise ValidationError("non_finite_number", "参数不能包含 NaN 或 Infinity。")


def parse_arguments(call: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """读取 OpenAI 格式 tool_call，并把 arguments 解码成对象。"""
    if not isinstance(call, dict):
        raise ValidationError("invalid_call", "工具调用格式无效。")
    call_id = call.get("id", "")
    if not isinstance(call_id, str) or not call_id or len(call_id) > 120:
        raise ValidationError("invalid_call_id", "工具调用编号无效。")
    if call.get("type") != "function":
        raise ValidationError("invalid_call_type", "只接受函数工具调用。")
    function = call.get("function")
    if not isinstance(function, dict):
        raise ValidationError("invalid_function", "工具调用内容无效。")
    name = function.get("name")
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        raise ValidationError("invalid_tool_name", "工具名称无效。")
    raw = function.get("arguments")
    if not isinstance(raw, str):
        raise ValidationError("invalid_arguments", "工具参数必须是 JSON 文本。")
    if len(raw.encode("utf-8", errors="replace")) > MAX_ARGUMENT_BYTES:
        raise ValidationError("arguments_too_large", "工具参数过大。")
    try:
        arguments = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except ValidationError:
        raise
    except (json.JSONDecodeError, RecursionError):
        raise ValidationError("invalid_json", "工具参数不是有效的 JSON。") from None
    if not isinstance(arguments, dict):
        raise ValidationError("arguments_not_object", "工具参数必须是 JSON 对象。")
    _walk_limits(arguments)
    return call_id, name, arguments


def _json_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return False


def validate_schema(value: Any, schema: dict[str, Any], path: str = "参数") -> None:
    """按工具自己的 JSON Schema 子集递归校验，不执行 Schema 中的内容。"""
    expected = schema.get("type")
    if expected and not _json_type(value, expected):
        raise ValidationError("wrong_type", f"{path}类型不正确，应为 {expected}。")

    if "enum" in schema and value not in schema["enum"]:
        raise ValidationError("invalid_value", f"{path}的值不在允许范围内。")

    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValidationError("too_short", f"{path}内容过短。")
        if len(value) > schema.get("maxLength", 8_000):
            raise ValidationError("too_long", f"{path}内容过长。")
        if schema.get("format") == "id" and not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value):
            raise ValidationError("invalid_id", f"{path}编号格式无效。")
        pattern = schema.get("pattern")
        if pattern and not re.fullmatch(pattern, value):
            raise ValidationError("invalid_value", f"{path}格式无效。")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValidationError("number_too_small", f"{path}小于允许范围。")
        if "maximum" in schema and value > schema["maximum"]:
            raise ValidationError("number_too_large", f"{path}超过允许范围。")

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ValidationError("too_few_items", f"{path}项目过少。")
        if len(value) > schema.get("maxItems", 100):
            raise ValidationError("too_many_items", f"{path}项目过多。")
        if schema.get("uniqueItems") and len({json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value}) != len(value):
            raise ValidationError("duplicate_item", f"{path}不能包含重复项目。")
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_schema(item, item_schema, f"{path}[{index}]")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                raise ValidationError("missing_field", f"缺少必填字段：{name}。")
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                # 不回显模型提供的字段名，避免把密钥或路径内容反射到模型响应。
                raise ValidationError("unknown_field", "包含不允许的参数字段。")
        for name, child in value.items():
            if name in properties:
                validate_schema(child, properties[name], name)


def check_tool_permission(name: str, permission: str, ctx: dict[str, Any]) -> None:
    """仅允许注册表声明的业务权限；调用方可进一步缩小本轮权限。"""
    if not name or not permission:
        raise ValidationError("permission_denied", "工具没有明确的权限声明。")
    if not isinstance(ctx, dict) or not isinstance(ctx.get("session_id"), str) or not ctx["session_id"]:
        raise ValidationError("invalid_context", "当前会话上下文无效。")
    if not isinstance(ctx.get("turn_id"), str) or not ctx["turn_id"]:
        raise ValidationError("invalid_context", "当前轮次上下文无效。")
    allowed = ctx.get("permissions")
    if allowed is not None and (not isinstance(allowed, (set, list, tuple)) or permission not in allowed):
        raise ValidationError("permission_denied", "当前会话没有执行该工具的权限。")
