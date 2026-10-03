"""ルールの検証と再計算（算出式の評価）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from . import expr
from .graph import CycleError, find_cycle, reachable, topo_sort
from .model import Param, Rule, check_value_type, normalize_number


@dataclass
class CalcResult:
    ref: str
    old_value: Any
    new_value: Any = None
    error: str | None = None

    @property
    def changed(self) -> bool:
        return self.error is None and self.new_value != self.old_value


def formula_rules(rules: Iterable[Rule]) -> list[Rule]:
    return [r for r in rules if r.kind == "formula" and r.enabled and r.expression and r.target]


def formula_edges(rules: Iterable[Rule]) -> list[tuple[str, str]]:
    """算出式による依存エッジ（参照元 → 代入先）。パースできない式は無視する。"""
    edges: list[tuple[str, str]] = []
    for r in formula_rules(rules):
        try:
            refs = expr.references(r.expression or "")
        except expr.ExprError:
            continue
        edges.extend((src, r.target) for src in sorted(refs))  # type: ignore[misc]
    return edges


def rule_refs(rule: Rule) -> set[str]:
    """ルールが関与するパラメータ参照（パース不能なら target のみ）。"""
    refs: set[str] = set()
    if rule.expression:
        try:
            refs |= expr.references(rule.expression)
        except expr.ExprError:
            pass
    if rule.target:
        refs.add(rule.target)
    return refs


def validate_rules(params: Iterable[Param], rules: Iterable[Rule]) -> dict[str, list[str]]:
    """ルール集合を検証し、ルールキー → エラー一覧を返す（問題がなければ空 dict）。

    検証内容: 構文、未定義参照、代入先の存在、代入先の重複、循環。
    """
    params = list(params)
    rules = list(rules)
    known = {p.ref for p in params}
    errors: dict[str, list[str]] = {}

    def add(key: str, msg: str) -> None:
        errors.setdefault(key, []).append(msg)

    targets: dict[str, str] = {}
    for r in rules:
        if r.kind not in ("formula", "constraint", "nl"):
            add(r.key, f"不明なルール種別です: {r.kind}")
            continue
        if r.kind in ("formula", "constraint"):
            if not r.expression:
                add(r.key, "式が空です")
            else:
                for msg in expr.check(r.expression, known):
                    add(r.key, msg)
        if r.kind == "formula":
            if not r.target:
                add(r.key, "算出式には代入先パラメータが必要です")
            elif r.target not in known:
                add(r.key, f"代入先が存在しません: {r.target}")
            elif r.enabled:
                if r.target in targets:
                    add(r.key, f"代入先 {r.target} には既に算出式 {targets[r.target]} があります")
                else:
                    targets[r.target] = r.key
                if r.expression and r.target in rule_refs(Rule(r.key, "constraint", r.expression)):
                    add(r.key, "算出式が自分自身を参照しています")
        if r.kind == "nl":
            if not (r.nl_text or "").strip():
                add(r.key, "自然言語ルールの本文が空です")
            if r.target and r.target not in known:
                add(r.key, f"対象パラメータが存在しません: {r.target}")

    edges = formula_edges(rules)
    cycle = find_cycle(sorted(known), edges)
    if cycle:
        msg = str(CycleError(cycle))
        cyc = set(cycle)
        for r in formula_rules(rules):
            if r.target in cyc:
                add(r.key, msg)
    return errors


def recalc(
    params: Iterable[Param],
    rules: Iterable[Rule],
    starts: Iterable[str] | None = None,
    include_starts: bool = True,
) -> dict[str, CalcResult]:
    """算出式を依存順に評価し、パラメータ参照 → CalcResult を返す。

    - ``starts`` が None なら、算出式を持つ全パラメータを対象とする。
    - ``starts`` 指定時は、その下流（``include_starts`` なら自身も）の算出対象のみを計算する。
    - 上流で新しく計算された値は、下流の計算に連鎖的に使われる（値自体は変更しない）。
    """
    params = list(params)
    by_ref = {p.ref: p for p in params}
    frules = {r.target: r for r in formula_rules(rules) if r.target in by_ref}
    edges = formula_edges(frules.values())
    order = topo_sort(sorted(by_ref), edges)  # 循環時は CycleError

    if starts is None:
        scope = set(frules)
    else:
        starts = list(starts)
        scope = reachable(starts, edges)
        if include_starts:
            scope |= set(starts)
        scope &= set(frules)

    env: dict[str, Any] = {p.ref: p.value for p in params}
    results: dict[str, CalcResult] = {}
    for ref in order:
        if ref not in scope:
            continue
        param = by_ref[ref]
        res = CalcResult(ref=ref, old_value=param.value)
        try:
            value = expr.evaluate(frules[ref].expression or "", env)
            if param.type == "number":
                value = normalize_number(value)
            type_err = check_value_type(param, value)
            if type_err:
                raise expr.EvalError(type_err)
            res.new_value = value
            env[ref] = value
        except expr.EvalError as e:
            res.error = str(e)
            env[ref] = None  # 下流は「値が未設定」として失敗させる
        results[ref] = res
    return results
