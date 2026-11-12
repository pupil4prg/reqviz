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
    d["aliases"] = loads(d.get("aliases"), [])
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


def lookup_params(ctx: Ctx, system_id: int, name: str) -> list[dict[str, Any]]:
    """参照名・キー・名前・別名のいずれかが name と一致するパラメータ（大文字小文字は区別しない）。

    別名は一意とは限らない（同じ実装名を複数のサーバーが持つ、など）ので、一致したものをすべて返す。
    """
    want = name.strip().casefold()
    out = []
    for p in list_params(ctx, system_id):
        matched = [m for m, v in (("ref", p["ref"]), ("key", p["key"]), ("name", p["name"])) if v.casefold() == want]
        matched += [f"alias:{a['label']}" if a["label"] else "alias" for a in p["aliases"] if a["value"].casefold() == want]
        if matched:
            out.append({**p, "matched": matched})
    return out


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


_ITEM_SQL = """
SELECT i.*, l.key AS layer_key, l.sort_order AS layer_order
FROM items i JOIN layers l ON l.id = i.layer_id
"""


def item_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["ref"] = f"{d['layer_key']}.{d['key']}"
    d["options"] = loads(d["options"], [])
    d["tags"] = loads(d["tags"], [])
    d["content"] = loads(d["content"])
    return d


def list_items(ctx: Ctx, system_id: int, kind: str | None = None) -> list[dict[str, Any]]:
    sql = _ITEM_SQL + " WHERE i.system_id = ?"
    args: list[Any] = [system_id]
    if kind:
        sql += " AND i.kind = ?"
        args.append(kind)
    rows = ctx.conn.execute(sql + " ORDER BY l.sort_order, l.id, i.id", args).fetchall()
    return [item_dict(r) for r in rows]


def get_item(ctx: Ctx, item_id: int) -> dict[str, Any]:
    r = ctx.conn.execute(_ITEM_SQL + " WHERE i.id = ?", (item_id,)).fetchone()
    if r is None:
        raise NotFound(f"構成項目が見つかりません: {item_id}")
    return item_dict(r)


def items_by_ref(ctx: Ctx, system_id: int) -> dict[str, dict[str, Any]]:
    return {i["ref"]: i for i in list_items(ctx, system_id)}


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


def choice_to_engine_param(i: dict[str, Any]) -> Param:
    """選択肢項目を、式から参照できる読み取り専用の enum 値として扱う（値 = 採用した選択肢のキー）。"""
    return Param(
        ref=i["ref"], type="enum", value=i["selected"],
        enum_options=[o["key"] for o in i["options"]],
        status="approved" if i["status"] == "approved" else "draft",
        assignable=False,
    )


def load_engine_model(ctx: Ctx, system_id: int) -> tuple[list[Param], list[Rule]]:
    """パラメータと、選択肢項目（式から参照可能・代入不可）を合わせたエンジン用モデル。"""
    params = [to_engine_param(p) for p in list_params(ctx, system_id)]
    params += [choice_to_engine_param(i) for i in list_items(ctx, system_id, "choice")]
    rules = [to_engine_rule(r) for r in list_rules(ctx, system_id)]
    return params, rules
