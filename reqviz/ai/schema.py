"""簡易 JSON スキーマ検証（type / required / properties / items / enum のみ）。"""

from __future__ import annotations

from typing import Any

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "integer": int,
    "boolean": bool,
    "null": type(None),
}


def validate(value: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    errors: list[str] = []
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        ok = False
        for name in types:
            if name == "any":
                ok = True
            elif name in ("number", "integer") and isinstance(value, bool):
                continue
            elif isinstance(value, _TYPES[name]):
                ok = True
        if not ok:
            return [f"{path}: {'/'.join(types)} が必要です"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {schema['enum']} のいずれかが必要です")
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errors.append(f"{path}.{k}: 必須です")
        for k, sub in schema.get("properties", {}).items():
            if k in value:
                errors.extend(validate(value[k], sub, f"{path}.{k}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errors
