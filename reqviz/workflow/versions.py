"""構成管理情報の履歴管理（エンティティ単位の版の系列、時点の再現、時点間の差分、復元）。

対象はパラメータ・構成項目・ルール。承認などの状態遷移は監査ログ側に残るため、
ここで扱うのは内容（値・選択肢・本文・式など）の履歴である。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..context import Ctx
from ..db import loads, transaction
from ..errors import Invalid
from ..repo import history
from ..repo import model as repo
from . import editing, items, snapshot

ENTITIES = ("param", "item", "rule")
KEY_FIELD = {"param": "ref", "item": "ref", "rule": "key"}
ENTITY_LABELS = {"param": "パラメータ", "item": "構成項目", "rule": "ルール"}


def _check_entity(entity: str | None) -> str:
    if entity not in ENTITIES:
        raise Invalid(f"entity は {' / '.join(ENTITIES)} のいずれかで指定してください: {entity!r}")
    return entity


# ---------------------------------------------------------------- 版の系列

def timeline(ctx: Ctx, system_id: int, entity: str, key: str) -> list[dict[str, Any]]:
    """1 つのエンティティの変更履歴を、版番号（1 始まり）つきで新しい順に返す。

    参照名（キー）の変更をさかのぼって、変更前の名前での履歴もつなげる。
    """
    _check_entity(entity)
    if not key:
        raise Invalid("key を指定してください")
    kf = KEY_FIELD[entity]
    chain: list[dict[str, Any]] = []
    cur_key, upper = key, None
    for _ in range(50):
        sql = "SELECT * FROM revisions WHERE system_id = ? AND entity = ? AND entity_key = ?"
        args: list[Any] = [system_id, entity, cur_key]
        if upper is not None:
            sql += " AND id < ?"
            args.append(upper)
        rows = [history.revision_dict(r) for r in ctx.conn.execute(sql + " ORDER BY id", args)]
        if not rows:
            break
        chain = rows + chain
        first = rows[0]
        prev_key = (first["before"] or {}).get(kf) if isinstance(first["before"], dict) else None
        if not prev_key or prev_key == cur_key:
            break
        cur_key, upper = prev_key, first["id"]
    for n, r in enumerate(chain, start=1):
        r["version"] = n
    return list(reversed(chain))


# ---------------------------------------------------------------- 時点の再現

def _normalize_time(text: str) -> str:
    try:
        dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise Invalid(f"日時は ISO 8601 形式で指定してください: {text!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def resolve_point(ctx: Ctx, system_id: int, point: str | None) -> int | None:
    """時点の指定を「そこまで適用済みのリビジョン ID」に変換する。None は現在。

    指定方法: 空 / ``now``（現在）、``rev:<ID>``（そのリビジョンの直後）、ISO 8601 の日時。
    """
    if point is None or point.strip() in ("", "now"):
        return None
    point = point.strip()
    if point.startswith("rev:"):
        try:
            return int(point[4:])
        except ValueError:
            raise Invalid(f"リビジョンの指定が不正です: {point}") from None
    at = _normalize_time(point)
    row = ctx.conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM revisions WHERE system_id = ? AND at <= ?", (system_id, at)
    ).fetchone()
    return int(row[0])


def _strip_status(state: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    for entity in state.values():
        for snap in entity.values():
            snap.pop("status", None)
    return state


def _undo(state: dict[str, dict[str, Any]], rev: dict[str, Any]) -> None:
    """state（リビジョン適用後）を、適用前の状態に戻す。"""
    entity, key = rev["entity"], rev["entity_key"]
    before, after = rev["before"], rev["after"]
    if entity in ENTITIES:
        kf = KEY_FIELD[entity]
        current = state[entity].pop(key, None)
        if before is None:
            return
        if not isinstance(before, dict):
            return
        merged = {**(current or {}), **before}
        merged.pop("status", None)
        prev_key = before.get(kf) or key
        merged[kf] = prev_key
        state[entity][prev_key] = merged
    elif entity == "layer" and isinstance(before, dict) and isinstance(after, dict):
        old, new = before.get("key"), after.get("key")
        if old and new and old != new:
            # レイヤーキーの変更は、配下の参照名（layer.key）をまとめて書き換える
            for ent in ("param", "item"):
                for ref in [r for r in state[ent] if r.startswith(new + ".")]:
                    snap = state[ent].pop(ref)
                    snap["ref"] = old + ref[len(new):]
                    state[ent][snap["ref"]] = snap


def state_at(ctx: Ctx, system_id: int, point: str | None = None) -> dict[str, Any]:
    """指定時点のパラメータ・構成項目・ルールの内容を、現在の状態から履歴を逆にたどって再現する。"""
    repo.get_system(ctx, system_id)
    cutoff = resolve_point(ctx, system_id, point)
    state = _strip_status(snapshot.entity_state(ctx, system_id))
    if cutoff is not None:
        rows = ctx.conn.execute(
            "SELECT * FROM revisions WHERE system_id = ? AND id > ? ORDER BY id DESC", (system_id, cutoff)
        )
        for r in rows:
            d = dict(r)
            d["before"], d["after"] = loads(d.pop("before_json")), loads(d.pop("after_json"))
            _undo(state, d)
    return {
        "revision": cutoff,
        "params": dict(sorted(state["param"].items())),
        "items": dict(sorted(state["item"].items())),
        "rules": dict(sorted(state["rule"].items())),
    }


def diff(ctx: Ctx, system_id: int, from_point: str | None, to_point: str | None = None) -> dict[str, Any]:
    """2 つの時点の間の差分（from → to）。to を省略すると現在。"""
    a = state_at(ctx, system_id, from_point)
    b = state_at(ctx, system_id, to_point)
    out: dict[str, Any] = {"from_revision": a["revision"], "to_revision": b["revision"]}
    for section in ("params", "items", "rules"):
        changes = []
        for key in sorted(set(a[section]) | set(b[section])):
            before, after = a[section].get(key), b[section].get(key)
            if before is None:
                changes.append({"key": key, "change": "added", "after": after})
            elif after is None:
                changes.append({"key": key, "change": "removed", "before": before})
            else:
                fields = history.diff_fields(before, after)
                if fields:
                    changes.append({"key": key, "change": "modified", "fields": fields})
        out[section] = changes
    return out


# ---------------------------------------------------------------- 復元

def _layer_id(ctx: Ctx, system_id: int, ref: str) -> tuple[int, str]:
    layer_key, _, key = ref.partition(".")
    layer = next((l for l in repo.list_layers(ctx, system_id) if l["key"] == layer_key), None)
    if layer is None:
        raise Invalid(f"レイヤー {layer_key} が存在しないため復元できません")
    return layer["id"], key


def restore_revision(ctx: Ctx, revision_id: int, comment: str = "") -> dict[str, Any]:
    """エンティティを、指定リビジョンの直後の内容に戻す（削除のリビジョンなら、削除前の内容で作り直す）。

    復元も通常の変更として履歴に残る。値が変わったパラメータ・構成項目は下書きに戻る。
    """
    rev = history.get_revision(ctx, revision_id)
    entity, sid = rev["entity"], rev["system_id"]
    if entity not in ENTITIES or sid is None:
        raise Invalid(f"この種類の履歴（{entity}）は個別に復元できません。版からの復元を使ってください")
    repo.get_system(ctx, sid)
    target = rev["after"] if rev["after"] is not None else rev["before"]
    kf = KEY_FIELD[entity]
    if not isinstance(target, dict) or not target.get(kf):
        raise Invalid("このリビジョンは一部のフィールドだけの記録のため、復元に使えません")
    key = target[kf]
    comment = comment or f"リビジョン #{revision_id} の内容に復元"

    with transaction(ctx.conn):
        if entity == "param":
            layer_id, pkey = _layer_id(ctx, sid, key)
            data = {k: target.get(k) for k in ("name", "type", "enum_options", "value", "unit", "source", "rationale", "ref_doc", "owner")}
            for extra in ("aliases", "tags"):  # 導入前に記録された履歴には含まれない（その場合は現状を維持）
                if target.get(extra) is not None:
                    data[extra] = target[extra]
            data.update(layer_id=layer_id, key=pkey)
            cur = repo.params_by_ref(ctx, sid).get(key)
            result = editing.update_param(ctx, cur["id"], data, comment) if cur else editing.create_param(ctx, sid, data, comment)
        elif entity == "item":
            layer_id, ikey = _layer_id(ctx, sid, key)
            fields = ("name", "tags", "rationale", "ref_doc", "owner") + (
                ("options", "selected") if target.get("kind") == "choice" else ("content_type", "content")
            )
            data = {k: target.get(k) for k in fields}
            data.update(layer_id=layer_id, key=ikey, kind=target.get("kind"))
            cur = repo.items_by_ref(ctx, sid).get(key)
            result = items.update_item(ctx, cur["id"], data, comment) if cur else items.create_item(ctx, sid, data, comment)
        else:
            data = {k: target.get(k) for k in editing.RULE_SNAPSHOT_FIELDS}
            cur = next((r for r in repo.list_rules(ctx, sid) if r["key"] == key), None)
            result = editing.update_rule(ctx, cur["id"], data, comment) if cur else editing.create_rule(ctx, sid, data, comment)
        history.audit(ctx, sid, "revision.restore", entity, key, {"revision_id": revision_id}, comment)
    return {"entity": entity, "key": key, "revision_id": revision_id, "result": result}
