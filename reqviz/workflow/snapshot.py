"""システム全体のスナップショット（版・テンプレート・JSON 入出力で共通の形式）。"""

from __future__ import annotations

from typing import Any

from ..context import Ctx
from ..db import dumps, now, transaction
from ..engine import calc
from ..errors import Invalid
from ..repo import history
from ..repo import model as repo
from . import editing

FORMAT = "reqviz/system"
RULES_FORMAT = "reqviz/rules"
FORMAT_VERSION = 1

PARAM_FIELDS = ("layer", "key", "name", "type", "enum_options", "value", "unit", "source", "rationale", "ref_doc", "owner")
RULE_FIELDS = ("key", "kind", "target", "expression", "nl_text", "severity", "description", "enabled")


def _rule_out(r: dict[str, Any]) -> dict[str, Any]:
    d = {k: r.get(k) for k in RULE_FIELDS}
    d["enabled"] = bool(d["enabled"])
    return d


def export_payload(ctx: Ctx, system_id: int, include_status: bool = True) -> dict[str, Any]:
    system = repo.get_system(ctx, system_id)
    layers = repo.list_layers(ctx, system_id)
    params = repo.list_params(ctx, system_id)
    out_params = []
    for p in sorted(params, key=lambda x: x["ref"]):
        d = {k: p.get(k) for k in PARAM_FIELDS if k != "layer"}
        d["layer"] = p["layer_key"]
        if include_status:
            d["status"] = p["status"]
            d["ai_skipped"] = p["ai_skipped"]
        out_params.append(d)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "system": {"name": system["name"], "description": system["description"]},
        "layers": [{"key": l["key"], "name": l["name"], "sort_order": i} for i, l in enumerate(layers)],
        "params": out_params,
        "rules": [_rule_out(r) for r in repo.list_rules(ctx, system_id)],
        "relations": sorted(
            ({"from": r["from_ref"], "to": r["to_ref"], "note": r["note"]} for r in repo.list_relations(ctx, system_id)),
            key=lambda x: (x["from"], x["to"]),
        ),
    }


def export_rules(ctx: Ctx, system_id: int) -> dict[str, Any]:
    return {
        "format": RULES_FORMAT,
        "format_version": FORMAT_VERSION,
        "rules": [_rule_out(r) for r in repo.list_rules(ctx, system_id)],
    }


def _require_format(payload: Any, fmt: str) -> None:
    if not isinstance(payload, dict) or payload.get("format") != fmt:
        raise Invalid(f"ファイル形式が不正です（format = {fmt} が必要）")
    if payload.get("format_version") != FORMAT_VERSION:
        raise Invalid(f"未対応のバージョンです: {payload.get('format_version')}")


def replace_with_payload(ctx: Ctx, system_id: int, payload: dict[str, Any], keep_status: bool, op: str, comment: str = "") -> None:
    """作業中の状態を payload の内容で置き換える（1 トランザクション）。"""
    _require_format(payload, FORMAT)
    with transaction(ctx.conn):
        old = export_payload(ctx, system_id)
        for t in ("relations", "rules", "params", "layers"):
            ctx.conn.execute(f"DELETE FROM {t} WHERE system_id = ?", (system_id,))

        layer_ids: dict[str, int] = {}
        for i, l in enumerate(sorted(payload.get("layers", []), key=lambda x: x.get("sort_order", 0))):
            key = editing.validate_identifier(l.get("key"), "レイヤーキー")
            if key in layer_ids:
                raise Invalid(f"レイヤーキーが重複しています: {key}")
            cur = ctx.conn.execute(
                "INSERT INTO layers (system_id, key, name, sort_order) VALUES (?, ?, ?, ?)",
                (system_id, key, l.get("name") or key, i),
            )
            layer_ids[key] = cur.lastrowid

        param_ids: dict[str, int] = {}
        for p in payload.get("params", []):
            layer = p.get("layer")
            if layer not in layer_ids:
                raise Invalid(f"パラメータ {p.get('key')} のレイヤー {layer} がありません")
            data = {k: p.get(k) for k in PARAM_FIELDS if k != "layer"}
            data["layer_id"] = layer_ids[layer]
            fields = editing._param_fields(ctx, system_id, data, None)
            ref = f"{layer}.{fields['key']}"
            if ref in param_ids:
                raise Invalid(f"パラメータが重複しています: {ref}")
            status = p.get("status", "draft") if keep_status else "draft"
            if status not in ("draft", "ai_evaluated", "in_review", "approved"):
                raise Invalid(f"状態が不正です: {status}")
            fields.update(
                system_id=system_id, status=status,
                ai_skipped=1 if (keep_status and p.get("ai_skipped")) else 0, updated_at=now(),
            )
            cols = ", ".join(fields)
            cur = ctx.conn.execute(
                f"INSERT INTO params ({cols}) VALUES ({', '.join('?' * len(fields))})", list(fields.values())
            )
            param_ids[ref] = cur.lastrowid

        for r in payload.get("rules", []):
            fields = editing._rule_fields(r, None)
            fields["system_id"] = system_id
            cols = ", ".join(fields)
            try:
                ctx.conn.execute(
                    f"INSERT INTO rules ({cols}) VALUES ({', '.join('?' * len(fields))})", list(fields.values())
                )
            except Exception as e:  # 一意制約違反など
                raise Invalid(f"ルール {fields['key']} を登録できません: {e}") from None

        for rel in payload.get("relations", []):
            f, t = rel.get("from"), rel.get("to")
            if f not in param_ids or t not in param_ids:
                raise Invalid(f"関連の端点が見つかりません: {f} -> {t}")
            ctx.conn.execute(
                "INSERT INTO relations (system_id, from_param, to_param, note) VALUES (?, ?, ?, ?)",
                (system_id, param_ids[f], param_ids[t], rel.get("note") or ""),
            )

        params, rules = repo.load_engine_model(ctx, system_id)
        errs = calc.validate_rules(params, rules)
        if errs:
            raise Invalid("ルールに誤りがあるため取り込めません", {"rule_errors": errs})

        history.add_revision(ctx, system_id, "system", "*", op, before=_summary(old), after=_summary(payload), comment=comment)
        ctx.conn.execute("UPDATE systems SET updated_at = ? WHERE id = ?", (now(), system_id))


def _summary(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: len(payload.get(k, [])) for k in ("layers", "params", "rules", "relations")}


def import_rules(ctx: Ctx, system_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    """ルール JSON を取り込む（キーで突き合わせて追加・更新。削除はしない）。"""
    _require_format(payload, RULES_FORMAT)
    existing = {r["key"]: r for r in repo.list_rules(ctx, system_id)}
    created, updated = [], []
    with transaction(ctx.conn):
        for r in payload.get("rules", []):
            key = r.get("key")
            if key in existing:
                cur = existing[key]
                incoming = {k: r[k] for k in RULE_FIELDS if k in r}
                if any(_rule_out(cur).get(k) != v for k, v in incoming.items()):
                    editing.update_rule(ctx, cur["id"], r)
                    updated.append(key)
            else:
                editing.create_rule(ctx, system_id, r)
                created.append(key)
        history.audit(ctx, system_id, "rules.import", "system", None, {"created": created, "updated": updated})
    return {"created": created, "updated": updated}


def diff_payloads(a: dict[str, Any], b: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """2 つのスナップショットの差分（a → b）。"""

    def keyed(items: list[dict[str, Any]], keyf) -> dict[str, dict[str, Any]]:
        return {keyf(x): x for x in items}

    specs = {
        "layers": lambda x: x["key"],
        "params": lambda x: f"{x['layer']}.{x['key']}",
        "rules": lambda x: x["key"],
        "relations": lambda x: f"{x['from']}->{x['to']}",
    }
    out: dict[str, list[dict[str, Any]]] = {}
    for section, keyf in specs.items():
        ka = keyed(a.get(section, []), keyf)
        kb = keyed(b.get(section, []), keyf)
        items = []
        for k in sorted(set(ka) | set(kb)):
            if k not in ka:
                items.append({"key": k, "change": "added", "after": kb[k]})
            elif k not in kb:
                items.append({"key": k, "change": "removed", "before": ka[k]})
            else:
                fields = history.diff_fields(ka[k], kb[k])
                if fields:
                    items.append({"key": k, "change": "modified", "fields": fields})
        out[section] = items
    return out


def payload_json(payload: dict[str, Any]) -> str:
    return dumps(payload)
