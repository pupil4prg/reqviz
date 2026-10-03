"""システム・レイヤー・パラメータ・関連・ルールの編集（履歴記録とフラグ伝播を含む）。"""

from __future__ import annotations

import keyword
import re
from typing import Any, Iterable

from ..context import Ctx
from ..db import dumps, now, transaction
from ..engine import calc, flags
from ..engine.model import PARAM_TYPES, RULE_KINDS, SEVERITIES, Param, check_value_type, normalize_number
from ..errors import Conflict, Invalid, NotFound
from ..repo import history
from ..repo import model as repo

RULE_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")

PARAM_SNAPSHOT_FIELDS = (
    "ref", "name", "type", "enum_options", "value", "unit", "source",
    "rationale", "ref_doc", "owner", "status",
)
RULE_SNAPSHOT_FIELDS = ("key", "kind", "target", "expression", "nl_text", "severity", "description", "enabled")


# ---------------------------------------------------------------- 共通

def validate_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.isidentifier() or keyword.iskeyword(value):
        raise Invalid(f"{label}は英字または _ で始まる英数字で指定してください（予約語は不可）: {value!r}")
    if len(value) > 64:
        raise Invalid(f"{label}が長すぎます（最大 64 文字）")
    return value


def require_text(data: dict[str, Any], field: str, label: str) -> str:
    v = data.get(field)
    if not isinstance(v, str) or not v.strip():
        raise Invalid(f"{label}は必須です")
    return v.strip()


def _update_row(ctx: Ctx, table: str, row_id: int, fields: dict[str, Any], row_version: int | None) -> None:
    """楽観ロック付き UPDATE。row_version が None ならロック確認をしない。"""
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    sql = f"UPDATE {table} SET {sets}, row_version = row_version + 1 WHERE id = ?"
    args = list(fields.values()) + [row_id]
    if row_version is not None:
        sql += " AND row_version = ?"
        args.append(row_version)
    cur = ctx.conn.execute(sql, args)
    if cur.rowcount == 0:
        exists = ctx.conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (row_id,)).fetchone()
        if not exists:
            raise NotFound(f"対象が見つかりません: {table}#{row_id}")
        raise Conflict("他の操作で更新されています。再読み込みしてからやり直してください")


def _touch_system(ctx: Ctx, system_id: int) -> None:
    ctx.conn.execute("UPDATE systems SET updated_at = ? WHERE id = ?", (now(), system_id))


def param_snapshot(p: dict[str, Any]) -> dict[str, Any]:
    return {k: p.get(k) for k in PARAM_SNAPSHOT_FIELDS}


def rule_snapshot(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r.get(k) for k in RULE_SNAPSHOT_FIELDS}


# ---------------------------------------------------------------- システム

def create_system(ctx: Ctx, data: dict[str, Any]) -> dict[str, Any]:
    name = require_text(data, "name", "システム名")
    ts = now()
    with transaction(ctx.conn):
        cur = ctx.conn.execute(
            "INSERT INTO systems (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, data.get("description") or "", ts, ts),
        )
        sid = cur.lastrowid
        history.audit(ctx, sid, "system.create", "system", name)
    return repo.get_system(ctx, sid)


def update_system(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "name" in data:
        fields["name"] = require_text(data, "name", "システム名")
    if "description" in data:
        fields["description"] = data.get("description") or ""
    fields["updated_at"] = now()
    with transaction(ctx.conn):
        _update_row(ctx, "systems", system_id, fields, data.get("row_version"))
    return repo.get_system(ctx, system_id)


def delete_system(ctx: Ctx, system_id: int) -> None:
    s = repo.get_system(ctx, system_id)
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM systems WHERE id = ?", (system_id,))
        history.audit(ctx, None, "system.delete", "system", s["name"], {"id": system_id})


# ---------------------------------------------------------------- レイヤー

def create_layer(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    repo.get_system(ctx, system_id)
    key = validate_identifier(data.get("key"), "レイヤーキー")
    name = require_text(data, "name", "レイヤー名")
    with transaction(ctx.conn):
        if ctx.conn.execute("SELECT 1 FROM layers WHERE system_id = ? AND key = ?", (system_id, key)).fetchone():
            raise Invalid(f"レイヤーキーが重複しています: {key}")
        order = data.get("sort_order")
        if order is None:
            order = ctx.conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM layers WHERE system_id = ?", (system_id,)
            ).fetchone()[0]
        cur = ctx.conn.execute(
            "INSERT INTO layers (system_id, key, name, sort_order) VALUES (?, ?, ?, ?)",
            (system_id, key, name, int(order)),
        )
        history.add_revision(ctx, system_id, "layer", key, "create", after={"key": key, "name": name})
        _touch_system(ctx, system_id)
    return repo.get_layer(ctx, cur.lastrowid)


def update_layer(ctx: Ctx, layer_id: int, data: dict[str, Any]) -> dict[str, Any]:
    layer = repo.get_layer(ctx, layer_id)
    sid = layer["system_id"]
    fields: dict[str, Any] = {}
    if "name" in data:
        fields["name"] = require_text(data, "name", "レイヤー名")
    new_key = layer["key"]
    if "key" in data and data["key"] != layer["key"]:
        new_key = validate_identifier(data["key"], "レイヤーキー")
        if ctx.conn.execute("SELECT 1 FROM layers WHERE system_id = ? AND key = ?", (sid, new_key)).fetchone():
            raise Invalid(f"レイヤーキーが重複しています: {new_key}")
        fields["key"] = new_key
    if "sort_order" in data:
        fields["sort_order"] = int(data["sort_order"])
    with transaction(ctx.conn):
        if new_key != layer["key"]:
            mapping = {
                f"{layer['key']}.{p['key']}": f"{new_key}.{p['key']}"
                for p in repo.list_params(ctx, sid)
                if p["layer_id"] == layer_id
            }
        else:
            mapping = {}
        _update_row(ctx, "layers", layer_id, fields, data.get("row_version"))
        if mapping:
            rename_refs(ctx, sid, mapping)
        after = repo.get_layer(ctx, layer_id)
        history.add_revision(
            ctx, sid, "layer", after["key"], "update",
            before={k: layer[k] for k in ("key", "name", "sort_order")},
            after={k: after[k] for k in ("key", "name", "sort_order")},
        )
        _touch_system(ctx, sid)
    return after


def delete_layer(ctx: Ctx, layer_id: int) -> None:
    layer = repo.get_layer(ctx, layer_id)
    n = ctx.conn.execute("SELECT COUNT(*) FROM params WHERE layer_id = ?", (layer_id,)).fetchone()[0]
    if n:
        raise Invalid(f"レイヤー {layer['key']} にはパラメータが {n} 件あるため削除できません")
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM layers WHERE id = ?", (layer_id,))
        history.add_revision(ctx, layer["system_id"], "layer", layer["key"], "delete", before={"key": layer["key"], "name": layer["name"]})
        _touch_system(ctx, layer["system_id"])


def reorder_layers(ctx: Ctx, system_id: int, layer_ids: list[int]) -> list[dict[str, Any]]:
    current = {l["id"] for l in repo.list_layers(ctx, system_id)}
    if set(layer_ids) != current or len(layer_ids) != len(current):
        raise Invalid("レイヤーの並び順には、全レイヤーの ID を 1 回ずつ指定してください")
    with transaction(ctx.conn):
        for i, lid in enumerate(layer_ids):
            ctx.conn.execute("UPDATE layers SET sort_order = ?, row_version = row_version + 1 WHERE id = ?", (i, lid))
        history.add_revision(ctx, system_id, "layer", "*", "update", after={"order": layer_ids}, comment="並べ替え")
        _touch_system(ctx, system_id)
    return repo.list_layers(ctx, system_id)


def rename_refs(ctx: Ctx, system_id: int, mapping: dict[str, str]) -> None:
    """ルールの式・代入先に含まれるパラメータ参照を置き換える。"""
    if not mapping:
        return
    pattern = re.compile(
        r"(?<![\w.])(" + "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)) + r")(?![\w])"
    )
    for r in repo.list_rules(ctx, system_id):
        fields: dict[str, Any] = {}
        if r["expression"]:
            new_expr = pattern.sub(lambda m: mapping[m.group(1)], r["expression"])
            if new_expr != r["expression"]:
                fields["expression"] = new_expr
        if r["target"] in mapping:
            fields["target"] = mapping[r["target"]]
        if fields:
            _update_row(ctx, "rules", r["id"], fields, None)
            history.add_revision(
                ctx, system_id, "rule", r["key"], "update",
                before={k: r[k] for k in fields}, after=fields, comment="参照名の変更に追従",
            )


# ---------------------------------------------------------------- パラメータ

def _coerce_value(p: Param, value: Any) -> Any:
    if p.type == "number" and isinstance(value, str) and value.strip() != "":
        try:
            value = float(value) if any(c in value for c in ".eE") else int(value)
        except ValueError:
            pass
    if p.type == "number":
        value = normalize_number(value)
    if value == "" and p.type != "string":
        value = None
    err = check_value_type(p, value)
    if err:
        raise Invalid(err)
    return value


def _param_fields(ctx: Ctx, system_id: int, data: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    """入力 dict を検証して DB 列の dict にする（current があれば部分更新）。"""
    cur = current or {}
    fields: dict[str, Any] = {}

    if "layer_id" in data or current is None:
        layer = repo.get_layer(ctx, int(data.get("layer_id") or 0)) if data.get("layer_id") else None
        if layer is None or layer["system_id"] != system_id:
            raise Invalid("レイヤーを指定してください")
        fields["layer_id"] = layer["id"]
    if "key" in data or current is None:
        fields["key"] = validate_identifier(data.get("key"), "パラメータキー")
    if "name" in data or current is None:
        fields["name"] = require_text(data, "name", "パラメータ名")
    ptype = data.get("type", cur.get("type", "number"))
    if ptype not in PARAM_TYPES:
        raise Invalid(f"型が不正です: {ptype}")
    if "type" in data or current is None:
        fields["type"] = ptype
    opts = data.get("enum_options", cur.get("enum_options", []))
    if not isinstance(opts, list) or not all(isinstance(o, str) for o in opts):
        raise Invalid("選択肢は文字列の配列で指定してください")
    if ptype == "enum" and not opts:
        raise Invalid("選択肢型には選択肢が 1 つ以上必要です")
    if "enum_options" in data or current is None:
        fields["enum_options"] = dumps(opts)
    if "value" in data or "type" in data or "enum_options" in data or current is None:
        value = data.get("value", cur.get("value"))
        probe = Param(ref="(value)", type=ptype, enum_options=opts)
        fields["value"] = dumps(_coerce_value(probe, value))
    for f in ("unit", "rationale", "ref_doc", "owner"):
        if f in data or current is None:
            v = data.get(f) or ""
            if not isinstance(v, str):
                raise Invalid(f"{f} は文字列で指定してください")
            fields[f] = v
    if "source" in data or current is None:
        src = data.get("source") or "manual"
        if src not in ("manual", "formula", "ai"):
            raise Invalid(f"値の出所が不正です: {src}")
        fields["source"] = src
    return fields


def _check_param_key_unique(ctx: Ctx, system_id: int, layer_id: int, key: str, exclude_id: int | None) -> None:
    row = ctx.conn.execute(
        "SELECT id FROM params WHERE system_id = ? AND layer_id = ? AND key = ?", (system_id, layer_id, key)
    ).fetchone()
    if row and row["id"] != exclude_id:
        raise Invalid(f"同じレイヤーにキー {key} のパラメータが既にあります")


def create_param(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    repo.get_system(ctx, system_id)
    fields = _param_fields(ctx, system_id, data, None)
    with transaction(ctx.conn):
        _check_param_key_unique(ctx, system_id, fields["layer_id"], fields["key"], None)
        fields.update(system_id=system_id, updated_at=now())
        cols = ", ".join(fields)
        cur = ctx.conn.execute(
            f"INSERT INTO params ({cols}) VALUES ({', '.join('?' * len(fields))})", list(fields.values())
        )
        p = repo.get_param(ctx, cur.lastrowid)
        history.add_revision(ctx, system_id, "param", p["ref"], "create", after=param_snapshot(p))
        # 算出式の代入先として既に参照されているなら要再計算
        targets = {r["target"] for r in repo.list_rules(ctx, system_id) if r["kind"] == "formula" and r["enabled"]}
        if p["ref"] in targets:
            ctx.conn.execute("UPDATE params SET flag_stale = 1 WHERE id = ?", (p["id"],))
        _touch_system(ctx, system_id)
    return repo.get_param(ctx, p["id"])


def update_param(ctx: Ctx, param_id: int, data: dict[str, Any], comment: str = "") -> dict[str, Any]:
    before = repo.get_param(ctx, param_id)
    sid = before["system_id"]
    fields = _param_fields(ctx, sid, data, before)
    with transaction(ctx.conn):
        layer_id = fields.get("layer_id", before["layer_id"])
        key = fields.get("key", before["key"])
        if layer_id != before["layer_id"] or key != before["key"]:
            _check_param_key_unique(ctx, sid, layer_id, key, param_id)
        value_changed = "value" in fields and fields["value"] != dumps(before["value"])
        if value_changed:
            if "source" not in data:
                fields["source"] = "manual"
            fields["flag_stale"] = 0
            fields["flag_upstream_changed"] = 0
            fields["proposed_value"] = None
            fields["proposed_error"] = None
            if before["status"] != "draft":
                fields["status"] = "draft"
        fields["updated_at"] = now()
        _update_row(ctx, "params", param_id, fields, data.get("row_version"))
        after = repo.get_param(ctx, param_id)
        if after["ref"] != before["ref"]:
            rename_refs(ctx, sid, {before["ref"]: after["ref"]})
        history.add_revision(
            ctx, sid, "param", after["ref"], "update",
            before=param_snapshot(before), after=param_snapshot(after), comment=comment,
        )
        if value_changed:
            if before["status"] != "draft":
                history.audit(
                    ctx, sid, "param.revert_to_draft", "param", after["ref"],
                    {"from": before["status"], "reason": "value_changed"},
                )
            propagate_value_change(ctx, sid, [after["ref"]])
        _touch_system(ctx, sid)
    return repo.get_param(ctx, param_id)


def propagate_value_change(ctx: Ctx, system_id: int, refs: Iterable[str]) -> flags.FlagUpdate:
    params, rules = repo.load_engine_model(ctx, system_id)
    statuses = {p.ref: p.status for p in params}
    upd = flags.propagate_change(refs, calc.formula_edges(rules), statuses)
    set_flags(ctx, system_id, upd.stale, upd.upstream_changed)
    return upd


def set_flags(ctx: Ctx, system_id: int, stale: Iterable[str], upstream_changed: Iterable[str]) -> None:
    by_ref = repo.params_by_ref(ctx, system_id)
    for ref in stale:
        if ref in by_ref:
            ctx.conn.execute("UPDATE params SET flag_stale = 1 WHERE id = ?", (by_ref[ref]["id"],))
    for ref in upstream_changed:
        if ref in by_ref:
            ctx.conn.execute("UPDATE params SET flag_upstream_changed = 1 WHERE id = ?", (by_ref[ref]["id"],))


def delete_param(ctx: Ctx, param_id: int) -> None:
    p = repo.get_param(ctx, param_id)
    sid = p["system_id"]
    users = []
    for r in repo.list_rules(ctx, sid):
        if p["ref"] in calc.rule_refs(repo.to_engine_rule(r)):
            users.append(r["key"])
    if users:
        raise Invalid(f"パラメータ {p['ref']} はルールから参照されているため削除できません", {"rules": users})
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM params WHERE id = ?", (param_id,))
        history.add_revision(ctx, sid, "param", p["ref"], "delete", before=param_snapshot(p))
        _touch_system(ctx, sid)


# ---------------------------------------------------------------- 関連（手動）

def create_relation(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    by_ref = repo.params_by_ref(ctx, system_id)
    f, t = data.get("from_ref"), data.get("to_ref")
    if f not in by_ref or t not in by_ref:
        raise Invalid("関連の両端には既存のパラメータを指定してください")
    if f == t:
        raise Invalid("同じパラメータ同士は関連付けできません")
    with transaction(ctx.conn):
        if ctx.conn.execute(
            "SELECT 1 FROM relations WHERE from_param = ? AND to_param = ?", (by_ref[f]["id"], by_ref[t]["id"])
        ).fetchone():
            raise Invalid("同じ関連が既にあります")
        cur = ctx.conn.execute(
            "INSERT INTO relations (system_id, from_param, to_param, note) VALUES (?, ?, ?, ?)",
            (system_id, by_ref[f]["id"], by_ref[t]["id"], data.get("note") or ""),
        )
        history.add_revision(ctx, system_id, "relation", f"{f}->{t}", "create", after={"note": data.get("note") or ""})
        _touch_system(ctx, system_id)
    return next(r for r in repo.list_relations(ctx, system_id) if r["id"] == cur.lastrowid)


def delete_relation(ctx: Ctx, relation_id: int) -> None:
    row = ctx.conn.execute("SELECT system_id FROM relations WHERE id = ?", (relation_id,)).fetchone()
    if row is None:
        raise NotFound(f"関連が見つかりません: {relation_id}")
    rel = next(r for r in repo.list_relations(ctx, row["system_id"]) if r["id"] == relation_id)
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM relations WHERE id = ?", (relation_id,))
        history.add_revision(
            ctx, rel["system_id"], "relation", f"{rel['from_ref']}->{rel['to_ref']}", "delete", before={"note": rel["note"]}
        )
        _touch_system(ctx, rel["system_id"])


# ---------------------------------------------------------------- ルール

def _rule_fields(data: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    cur = current or {}
    merged = {k: data.get(k, cur.get(k)) for k in RULE_SNAPSHOT_FIELDS}
    if not isinstance(merged["key"], str) or not RULE_KEY_RE.match(merged["key"]):
        raise Invalid("ルールキーは英数字・_・-・. で 64 文字以内で指定してください")
    if merged["kind"] not in RULE_KINDS:
        raise Invalid(f"ルール種別が不正です: {merged['kind']}")
    merged["severity"] = merged["severity"] or "error"
    if merged["severity"] not in SEVERITIES:
        raise Invalid(f"重要度が不正です: {merged['severity']}")
    merged["enabled"] = 1 if merged["enabled"] in (None, True, 1) else 0
    for f in ("expression", "nl_text", "target"):
        if isinstance(merged[f], str):
            merged[f] = merged[f].strip() or None
    merged["description"] = merged["description"] or ""
    if merged["kind"] == "constraint":
        merged["target"] = None
    if merged["kind"] == "nl":
        merged["expression"] = None
    else:
        merged["nl_text"] = None
    return merged


def check_rule(ctx: Ctx, system_id: int, data: dict[str, Any], exclude_id: int | None = None) -> list[str]:
    """ルール案を、既存ルールと合わせて検証する（保存はしない）。"""
    fields = _rule_fields(data, None)
    params, rules = repo.load_engine_model(ctx, system_id)
    existing = repo.list_rules(ctx, system_id)
    others = [repo.to_engine_rule(r) for r in existing if r["id"] != exclude_id]
    if any(r.key == fields["key"] for r in others):
        return [f"ルールキーが重複しています: {fields['key']}"]
    candidate = repo.to_engine_rule(fields)
    errs = calc.validate_rules(params, others + [candidate])
    return errs.get(candidate.key, [])


def _after_rule_change(ctx: Ctx, system_id: int, targets: Iterable[str | None]) -> None:
    """算出式の追加・変更・削除で影響を受ける代入先と、その下流に要再計算を付ける。"""
    refs = {t for t in targets if t}
    if not refs:
        return
    params, rules = repo.load_engine_model(ctx, system_id)
    statuses = {p.ref: p.status for p in params}
    current_targets = {r.target for r in calc.formula_rules(rules)}
    # 算出式がなくなった代入先は手入力値として扱い、要再計算と再計算案を解除する
    by_ref = repo.params_by_ref(ctx, system_id)
    for ref in refs - current_targets:
        if ref in by_ref:
            ctx.conn.execute(
                "UPDATE params SET flag_stale = 0, proposed_value = NULL, proposed_error = NULL WHERE id = ?",
                (by_ref[ref]["id"],),
            )
    live = refs & current_targets
    if not live:
        return
    upd = flags.propagate_change(live, calc.formula_edges(rules), statuses)
    stale = set(upd.stale) | live
    upstream = set(upd.upstream_changed) | {r for r in live if statuses.get(r) == "approved"}
    set_flags(ctx, system_id, stale, upstream)


def create_rule(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    repo.get_system(ctx, system_id)
    fields = _rule_fields(data, None)
    with transaction(ctx.conn):
        errs = check_rule(ctx, system_id, fields)
        if errs:
            raise Invalid("ルールに誤りがあります", {"errors": errs})
        fields["system_id"] = system_id
        cols = ", ".join(fields)
        cur = ctx.conn.execute(
            f"INSERT INTO rules ({cols}) VALUES ({', '.join('?' * len(fields))})", list(fields.values())
        )
        r = repo.get_rule(ctx, cur.lastrowid)
        history.add_revision(ctx, system_id, "rule", r["key"], "create", after=rule_snapshot(r))
        if r["kind"] == "formula" and r["enabled"]:
            _after_rule_change(ctx, system_id, [r["target"]])
        _touch_system(ctx, system_id)
    return r


def update_rule(ctx: Ctx, rule_id: int, data: dict[str, Any]) -> dict[str, Any]:
    before = repo.get_rule(ctx, rule_id)
    sid = before["system_id"]
    fields = _rule_fields(data, before)
    with transaction(ctx.conn):
        errs = check_rule(ctx, sid, fields, exclude_id=rule_id)
        if errs:
            raise Invalid("ルールに誤りがあります", {"errors": errs})
        changed = {k: v for k, v in fields.items() if v != (int(before[k]) if k == "enabled" else before[k])}
        _update_row(ctx, "rules", rule_id, changed, data.get("row_version"))
        after = repo.get_rule(ctx, rule_id)
        history.add_revision(ctx, sid, "rule", after["key"], "update", before=rule_snapshot(before), after=rule_snapshot(after))
        affected = []
        if before["kind"] == "formula" and before["enabled"]:
            affected.append(before["target"])
        if after["kind"] == "formula" and after["enabled"]:
            affected.append(after["target"])
        if changed:
            _after_rule_change(ctx, sid, affected)
        _touch_system(ctx, sid)
    return after


def delete_rule(ctx: Ctx, rule_id: int) -> None:
    r = repo.get_rule(ctx, rule_id)
    sid = r["system_id"]
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
        history.add_revision(ctx, sid, "rule", r["key"], "delete", before=rule_snapshot(r))
        if r["kind"] == "formula" and r["enabled"]:
            _after_rule_change(ctx, sid, [r["target"]])
        _touch_system(ctx, sid)
