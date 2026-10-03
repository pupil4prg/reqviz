"""エンジン層で使う軽量データ型（DB 非依存）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PARAM_TYPES = ("number", "string", "enum", "bool")
RULE_KINDS = ("formula", "constraint", "nl")
SEVERITIES = ("error", "warning")


@dataclass
class Param:
    ref: str  # "layer.key"
    type: str = "number"
    value: Any = None
    enum_options: list[str] = field(default_factory=list)
    status: str = "draft"


@dataclass
class Rule:
    key: str
    kind: str
    expression: str | None = None
    target: str | None = None  # formula の代入先 / nl の対象（参照名）
    severity: str = "error"
    enabled: bool = True
    nl_text: str | None = None
    description: str | None = None


def check_value_type(param: Param, value: Any) -> str | None:
    """値が型に合致するか確認し、合致しなければエラーメッセージを返す。"""
    if value is None:
        return None
    t = param.type
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{param.ref}: 数値が必要です（{value!r}）"
    elif t == "string":
        if not isinstance(value, str):
            return f"{param.ref}: 文字列が必要です（{value!r}）"
    elif t == "enum":
        if not isinstance(value, str) or (param.enum_options and value not in param.enum_options):
            opts = ", ".join(param.enum_options)
            return f"{param.ref}: 選択肢（{opts}）のいずれかが必要です（{value!r}）"
    elif t == "bool":
        if not isinstance(value, bool):
            return f"{param.ref}: 真偽値が必要です（{value!r}）"
    return None


def normalize_number(value: Any) -> Any:
    """整数値の float は int に正規化する（表示・比較の安定化）。"""
    if isinstance(value, float) and value.is_integer() and abs(value) < 2**53:
        return int(value)
    return value
