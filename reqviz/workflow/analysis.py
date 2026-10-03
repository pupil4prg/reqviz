"""グラフ構築・制約違反・再計算案の生成と適用。"""

from __future__ import annotations

from typing import Any, Iterable

from ..context import Ctx
from ..db import dumps, now, transaction
from ..engine import calc, layout
from ..engine.graph import CycleError
from ..engine.constraints import Violation, evaluate_constraints
from ..errors import Invalid
from ..repo import history
from ..repo import model as repo
from . import editing


def violations(ctx: Ctx, system_id: int) -> list[Violation]:
    params, rules = repo.load_engine_model(ctx, system_id)
    values = {p.ref: p.value for p in params}
    return evaluate_constraints(values, rules)


def graph(ctx: Ctx, system_id: int) -> dict[str, Any]:
    """グラフ表示用データ（ノード・ルールノード・エッジ・違反）をまとめて返す。"""
    system = repo.get_system(ctx, system_id)
    layers = repo.list_layers(ctx, system_id)
    params = repo.list_params(ctx, system_id)
    rules = repo.list_rules(ctx, system_id)
    relations = repo.list_relations(ctx, system_id)
    by_ref = {p["ref"]: p for p in params}
    layer_order = {l["key"]: l["sort_order"] for l in layers}

    viols = violations(ctx, system_id)
    worst: dict[str, str] = {}
    for v in viols:
        for ref in v.refs:
            if worst.get(ref) != "error":
                worst[ref] = v.severity

    formula_by_target = {r["target"]: r for r in rules if r["kind"] == "formula" and r["enabled"]}
    nl_by_target: dict[str, list[str]] = {}
    for r in rules:
        if r["kind"] == "nl" and r["target"]:
            nl_by_target.setdefault(r["target"], []).append(r["key"])

    nodes = []
    for p in params:
        f = formula_by_target.get(p["ref"])
        nodes.append({
            **p,
            "formula_rule": f["key"] if f else None,
            "formula": f["expression"] if f else None,
            "nl_rules": nl_by_target.get(p["ref"], []),
            "violation": worst.get(p["ref"]),
        })

    edges: list[dict[str, Any]] = []
    for r in rules:
        if not r["enabled"] or r["kind"] == "nl":
            continue
        refs = sorted(calc.rule_refs(repo.to_engine_rule(r)) & set(by_ref))
        if r["kind"] == "formula":
            for src in refs:
                if src != r["target"]:
                    edges.append({"from": src, "to": r["target"], "kind": "derives", "rule_key": r["key"]})
    rule_nodes = []
    viol_by_rule = {v.rule_key: v for v in viols}
    for r in rules:
        if r["kind"] != "constraint" or not r["enabled"]:
            continue
        refs = sorted(calc.rule_refs(repo.to_engine_rule(r)) & set(by_ref))
        if not refs:
            continue
        layer_key = max((by_ref[x]["layer_key"] for x in refs), key=lambda k: layer_order.get(k, 0))
        v = viol_by_rule.get(r["key"])
        rid = f"rule:{r['key']}"
        rule_nodes.append({
            "id": rid, "key": r["key"], "kind": "constraint", "severity": r["severity"],
            "expression": r["expression"], "description": r["description"], "layer_key": layer_key,
            "violation": v.to_dict() if v else None,
        })
        for src in refs:
            edges.append({"from": src, "to": rid, "kind": "constrains", "rule_key": r["key"]})
    for rel in relations:
        edges.append({"from": rel["from_ref"], "to": rel["to_ref"], "kind": "influences", "relation_id": rel["id"], "note": rel["note"]})

    lay = layout.layout(
        [l["key"] for l in layers],
        [(n["ref"], n["layer_key"]) for n in nodes] + [(rn["id"], rn["layer_key"]) for rn in rule_nodes],
        [(e["from"], e["to"]) for e in edges],
    )
    for n in nodes:
        n["x"], n["y"] = lay.positions.get(n["ref"], (0, 0))
    for rn in rule_nodes:
        rn["x"], rn["y"] = lay.positions.get(rn["id"], (0, 0))

    return {
        "system": system,
        "layers": layers,
        "columns": [{"layer_key": k, "x": x} for k, x in lay.columns],
        "size": {"width": lay.width, "height": lay.height},
        "nodes": nodes,
        "rule_nodes": rule_nodes,
        "edges": edges,
        "violations": [v.to_dict() for v in viols],
        "rule_errors": calc.validate_rules(*repo.load_engine_model(ctx, system_id)),
    }


def recalc(ctx: Ctx, system_id: int, mode: str = "stale", refs: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """再計算案を生成して保存する（値は変更しない）。

    mode: ``stale``（要再計算のものとその下流）/ ``downstream``（refs の下流と自身）/ ``all``
    """
    params, rules = repo.load_engine_model(ctx, system_id)
    by_ref = repo.params_by_ref(ctx, system_id)
    if mode == "all":
        starts = None
    elif mode == "stale":
        starts = [r for r, p in by_ref.items() if p["flag_stale"]]
    elif mode == "downstream":
        starts = list(refs or [])
        unknown = [r for r in starts if r not in by_ref]
        if unknown:
            raise Invalid(f"存在しないパラメータです: {', '.join(unknown)}")
    else:
        raise Invalid(f"再計算モードが不正です: {mode}")
    if starts == []:
        return []
    try:
        results = calc.recalc(params, rules, starts=starts)
    except CycleError as e:
        raise Invalid(str(e)) from None

    out = []
    with transaction(ctx.conn):
        for ref, res in results.items():
            pid = by_ref[ref]["id"]
            if res.error:
                ctx.conn.execute(
                    "UPDATE params SET proposed_value = NULL, proposed_error = ?, flag_stale = 1 WHERE id = ?",
                    (res.error, pid),
                )
                status = "error"
            elif res.changed:
                ctx.conn.execute(
                    "UPDATE params SET proposed_value = ?, proposed_error = NULL, flag_stale = 1 WHERE id = ?",
                    (dumps(res.new_value), pid),
                )
                status = "proposed"
            else:
                ctx.conn.execute(
                    "UPDATE params SET proposed_value = NULL, proposed_error = NULL, flag_stale = 0 WHERE id = ?",
                    (pid,),
                )
                status = "unchanged"
            out.append({"ref": ref, "status": status, "old_value": res.old_value, "new_value": res.new_value, "error": res.error})
        history.audit(ctx, system_id, "recalc", "system", None, {"mode": mode, "count": len(out)})
    return out


def apply_proposals(ctx: Ctx, system_id: int, refs: Iterable[str] | None = None, comment: str = "") -> list[dict[str, Any]]:
    """再計算案を適用する。承認済み等のパラメータは下書きに戻す。refs が None なら全件。"""
    by_ref = repo.params_by_ref(ctx, system_id)
    targets = [p for p in by_ref.values() if p["has_proposal"]]
    if refs is not None:
        wanted = set(refs)
        targets = [p for p in targets if p["ref"] in wanted]
    if not targets:
        return []
    applied = []
    with transaction(ctx.conn):
        for p in targets:
            new_status = "draft"
            ctx.conn.execute(
                """UPDATE params SET value = ?, source = 'formula', flag_stale = 0, flag_upstream_changed = 0,
                   proposed_value = NULL, proposed_error = NULL, status = ?, updated_at = ?,
                   row_version = row_version + 1 WHERE id = ?""",
                (dumps(p["proposed_value"]), new_status, now(), p["id"]),
            )
            after = repo.get_param(ctx, p["id"])
            history.add_revision(
                ctx, system_id, "param", p["ref"], "update",
                before=editing.param_snapshot(p), after=editing.param_snapshot(after),
                comment=comment or "再計算案を適用",
            )
            if p["status"] != "draft":
                history.audit(
                    ctx, system_id, "param.revert_to_draft", "param", p["ref"],
                    {"from": p["status"], "reason": "recalc_applied"},
                )
            applied.append(p["ref"])
        upd = editing.propagate_value_change(ctx, system_id, applied)
        # 同時に適用したパラメータは最新の上流値で計算済みなので要再計算を外す
        for ref in set(applied) & upd.stale:
            ctx.conn.execute("UPDATE params SET flag_stale = 0 WHERE id = ?", (by_ref[ref]["id"],))
        history.audit(ctx, system_id, "recalc.apply", "system", None, {"refs": applied}, comment)
    return [repo.get_param(ctx, by_ref[r]["id"]) for r in applied]
