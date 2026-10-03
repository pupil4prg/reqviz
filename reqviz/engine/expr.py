"""安全な式評価器。

Python の式構文のサブセットを ``ast`` でパースし、ホワイトリストに含まれる
ノードのみを評価する。``eval`` / ``exec`` は使わない。

パラメータ参照は ``layer.key`` 形式（例: ``app.server_count``）。
"""

from __future__ import annotations

import ast
import math
import operator
from functools import lru_cache
from typing import Any, Callable, Mapping

MAX_LENGTH = 1000
MAX_DEPTH = 30
MAX_EXPONENT = 100


class ExprError(Exception):
    """式の構文・検証エラー。"""


class EvalError(Exception):
    """式の評価時エラー（未設定の参照、0 除算、型不一致など）。"""


def _sum(*args: Any) -> Any:
    return sum(args)


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "ceil": math.ceil,
    "floor": math.floor,
    "round": round,
    "min": min,
    "max": max,
    "abs": abs,
    "sum": _sum,
}

_BINOPS: dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARYOPS: dict[type, Callable[[Any], Any]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
    ast.Not: operator.not_,
}

_CMPOPS: dict[type, Callable[[Any, Any], bool]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def _ref_name(node: ast.AST) -> str | None:
    """``layer.key`` 形式の Attribute ノードなら参照名を返す。"""
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and isinstance(node.ctx, ast.Load)
    ):
        return f"{node.value.id}.{node.attr}"
    return None


def _validate(node: ast.AST, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ExprError("式のネストが深すぎます")
    d = depth + 1

    if isinstance(node, ast.Expression):
        _validate(node.body, d)
    elif isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float, str, bool)) and node.value is not None:
            raise ExprError(f"使用できない定数です: {node.value!r}")
    elif isinstance(node, ast.Attribute):
        if _ref_name(node) is None:
            raise ExprError("パラメータ参照は「レイヤー.キー」の形式で記述してください")
    elif isinstance(node, ast.Name):
        raise ExprError(
            f"不明な名前です: {node.id}（パラメータは「レイヤー.キー」で参照してください）"
        )
    elif isinstance(node, ast.BinOp):
        if type(node.op) not in _BINOPS:
            raise ExprError("使用できない演算子です")
        _validate(node.left, d)
        _validate(node.right, d)
    elif isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARYOPS:
            raise ExprError("使用できない演算子です")
        _validate(node.operand, d)
    elif isinstance(node, ast.BoolOp):
        for v in node.values:
            _validate(v, d)
    elif isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _CMPOPS:
                raise ExprError("使用できない比較演算子です")
        _validate(node.left, d)
        for c in node.comparators:
            _validate(c, d)
    elif isinstance(node, ast.IfExp):
        _validate(node.test, d)
        _validate(node.body, d)
        _validate(node.orelse, d)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            _validate(e, d)
    elif isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            names = ", ".join(sorted(FUNCTIONS))
            raise ExprError(f"使用できない関数です（使用可能: {names}）")
        if node.keywords:
            raise ExprError("関数のキーワード引数は使用できません")
        for a in node.args:
            if isinstance(a, ast.Starred):
                raise ExprError("可変長引数は使用できません")
            _validate(a, d)
    else:
        raise ExprError(f"使用できない構文です: {type(node).__name__}")


@lru_cache(maxsize=1024)
def parse(text: str) -> ast.Expression:
    """式をパース・検証して AST を返す。"""
    if not isinstance(text, str) or not text.strip():
        raise ExprError("式が空です")
    if len(text) > MAX_LENGTH:
        raise ExprError(f"式が長すぎます（最大 {MAX_LENGTH} 文字）")
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"構文エラー: {e.msg}") from None
    _validate(tree)
    return tree


def references(text: str) -> set[str]:
    """式が参照しているパラメータ名の集合を返す。"""
    tree = parse(text)
    refs: set[str] = set()
    for node in ast.walk(tree):
        name = _ref_name(node)
        if name is not None:
            refs.add(name)
    return refs


def check(text: str, known_refs: set[str] | None = None) -> list[str]:
    """式を検証し、エラーメッセージの一覧を返す（問題なければ空）。"""
    try:
        refs = references(text)
    except ExprError as e:
        return [str(e)]
    if known_refs is None:
        return []
    return [f"未定義のパラメータです: {r}" for r in sorted(refs - known_refs)]


def evaluate(text: str, env: Mapping[str, Any]) -> Any:
    """式を評価する。``env`` は参照名 → 値のマッピング。"""
    tree = parse(text)
    try:
        return _eval(tree.body, env)
    except EvalError:
        raise
    except ZeroDivisionError:
        raise EvalError("0 で除算しました") from None
    except (TypeError, ValueError, OverflowError) as e:
        raise EvalError(f"評価エラー: {e}") from None


def _eval(node: ast.AST, env: Mapping[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute):
        name = _ref_name(node)
        assert name is not None
        if name not in env:
            raise EvalError(f"未定義のパラメータです: {name}")
        value = env[name]
        if value is None:
            raise EvalError(f"値が未設定です: {name}")
        return value
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, env)
        right = _eval(node.right, env)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)):
            if abs(right) > MAX_EXPONENT:
                raise EvalError(f"指数が大きすぎます（最大 {MAX_EXPONENT}）")
        return _BINOPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp):
        return _UNARYOPS[type(node.op)](_eval(node.operand, env))
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = _eval(v, env)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = _eval(v, env)
            if result:
                return result
        return result
    if isinstance(node, ast.Compare):
        left = _eval(node.left, env)
        for op, comp in zip(node.ops, node.comparators):
            right = _eval(comp, env)
            if not _CMPOPS[type(op)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.IfExp):
        return _eval(node.body, env) if _eval(node.test, env) else _eval(node.orelse, env)
    if isinstance(node, (ast.Tuple, ast.List)):
        return tuple(_eval(e, env) for e in node.elts)
    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)
        args = [_eval(a, env) for a in node.args]
        return FUNCTIONS[node.func.id](*args)
    raise EvalError(f"評価できない構文です: {type(node).__name__}")  # pragma: no cover
