"""システム・レイヤー・パラメータ・ルール・関連の読み出しと行変換。"""

from __future__ import annotations

from typing import Any

from ..context import Ctx
from ..db import loads
from ..engine.model import Param, Rule
from ..errors import NotFound


def system_dict(r: Any) -> dict[str, Any]:
    return dict(r)


def get_system(ctx: Ctx, system_id: int) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM systems WHERE id = ?", (system_id,)).fetchone()
    if r is None:
        raise NotFound(f"システムが見つかりません: {system_id}")
    return system_dict(r)


def list_systems(ctx: Ctx) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        """
        SELECT s.*,
               (SELECT COUNT(*) FROM params p WHERE p.system_id = s.id) AS param_count,
               (SELECT COUNT(*) FROM params p WHERE p.system_id = s.id AND p.status = 'approved') AS approved_count,
               b.version_no AS current_baseline_version
        FROM systems s LEFT JOIN baselines b ON b.id = s.current_baseline_id
        ORDER BY s.id
        """
    ).fetchall()
    return [dict(r) for r in rows]


def list_layers(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        "SELECT * FROM layers WHERE system_id = ? ORDER BY sort_order, id", (system_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_layer(ctx: Ctx, layer_id: int) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM layers WHERE id = ?", (layer_id,)).fetchone()
    if r is None:
        raise NotFound(f"レイヤーが見つかりません: {layer_id}")
    return dict(r)


_PARAM_SQL = """
SELECT p.*, l.key AS layer_key, l.sort_order AS layer_order
FROM params p JOIN layers l ON l.id = p.layer_id
"""


def param_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["ref"] = f"{d['layer_key']}.{d['key']}"
    d["enum_options"] = loads(d["enum_options"], [])
    d["value"] = loads(d["value"])
    d["has_proposal"] = d["proposed_value"] is not None
    d["proposed_value"] = loads(d["proposed_value"])
    for k in ("ai_skipped", "flag_stale", "flag_upstream_changed"):
        d[k] = bool(d[k])
    return d


def list_params(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        _PARAM_SQL + " WHERE p.system_id = ? ORDER BY l.sort_order, l.id, p.id", (system_id,)
    ).fetchall()
    return [param_dict(r) for r in rows]


def get_param(ctx: Ctx, param_id: int) -> dict[str, Any]:
    r = ctx.conn.execute(_PARAM_SQL + " WHERE p.id = ?", (param_id,)).fetchone()
    if r is None:
        raise NotFound(f"パラメータが見つかりません: {param_id}")
    return param_dict(r)


def params_by_ref(ctx: Ctx, system_id: int) -> dict[str, dict[str, Any]]:
    return {p["ref"]: p for p in list_params(ctx, system_id)}


def rule_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["enabled"] = bool(d["enabled"])
    return d


def list_rules(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    rows = ctx.conn.execute("SELECT * FROM rules WHERE system_id = ? ORDER BY key", (system_id,)).fetchall()
    return [rule_dict(r) for r in rows]


def get_rule(ctx: Ctx, rule_id: int) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()
    if r is None:
        raise NotFound(f"ルールが見つかりません: {rule_id}")
    return rule_dict(r)


def list_relations(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        """
        SELECT r.*, lf.key || '.' || pf.key AS from_ref, lt.key || '.' || pt.key AS to_ref
        FROM relations r
        JOIN params pf ON pf.id = r.from_param JOIN layers lf ON lf.id = pf.layer_id
        JOIN params pt ON pt.id = r.to_param JOIN layers lt ON lt.id = pt.layer_id
        WHERE r.system_id = ? ORDER BY r.id
        """,
        (system_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def to_engine_param(p: dict[str, Any]) -> Param:
    return Param(ref=p["ref"], type=p["type"], value=p["value"], enum_options=p["enum_options"], status=p["status"])


def to_engine_rule(r: dict[str, Any]) -> Rule:
    return Rule(
        key=r["key"],
        kind=r["kind"],
        expression=r.get("expression"),
        target=r.get("target"),
        severity=r.get("severity") or "error",
        enabled=bool(r.get("enabled", True)),
        nl_text=r.get("nl_text"),
        description=r.get("description"),
    )


def load_engine_model(ctx: Ctx, system_id: int) -> tuple[list[Param], list[Rule]]:
    params = [to_engine_param(p) for p in list_params(ctx, system_id)]
    rules = [to_engine_rule(r) for r in list_rules(ctx, system_id)]
    return params, rules
