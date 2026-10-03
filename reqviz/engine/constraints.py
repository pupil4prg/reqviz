"""制約ルールの評価。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import expr
from .calc import rule_refs
from .model import Rule


@dataclass
class Violation:
    rule_key: str
    severity: str  # error / warning
    kind: str  # "violated"（式が偽） / "eval_error"（評価できない）
    message: str
    refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_key": self.rule_key,
            "severity": self.severity,
            "kind": self.kind,
            "message": self.message,
            "refs": self.refs,
        }


def evaluate_constraints(values: Mapping[str, Any], rules: Iterable[Rule]) -> list[Violation]:
    """有効な制約ルールを評価し、違反（評価不能を含む）の一覧を返す。"""
    violations: list[Violation] = []
    for r in rules:
        if r.kind != "constraint" or not r.enabled or not r.expression:
            continue
        refs = sorted(rule_refs(r))
        try:
            ok = expr.evaluate(r.expression, values)
        except (expr.ExprError, expr.EvalError) as e:
            violations.append(Violation(r.key, r.severity, "eval_error", f"評価できません: {e}", refs))
            continue
        if not ok:
            detail = ", ".join(f"{k}={values.get(k)!r}" for k in refs)
            label = r.description or r.expression
            violations.append(
                Violation(r.key, r.severity, "violated", f"制約違反: {label}（{detail}）", refs)
            )
    return violations
