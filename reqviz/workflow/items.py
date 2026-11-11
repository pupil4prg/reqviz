"""構成項目（パラメータ以外の構成管理対象）の編集と状態遷移。

- ``choice``: 列挙データ。実装方式などの選択肢（説明・長所・短所つき）と、採用した選択肢。
  採用した選択肢のキーは、式から ``layer.key == 'redis'`` のように参照できる（代入は不可）。
- ``document``: 不定形データ。テキスト / Markdown / YAML / 任意の JSON。
"""

from __future__ import annotations

import json
from typing import Any

from ..context import Ctx
from ..db import dumps, now, transaction
from ..engine import calc
from ..errors import Invalid
from ..repo import history
from ..repo import model as repo
from . import editing

ITEM_KINDS = ("choice", "document")
CONTENT_TYPES = ("text", "markdown", "json", "yaml")
MAX_CONTENT_BYTES = 1_000_000
MAX_OPTIONS = 100

KIND_LABELS = {"choice": "選択肢", "document": "不定形データ"}
STATUS_LABELS = {"draft": "下書き", "in_review": "レビュー中", "approved": "承認済み"}

ITEM_SNAPSHOT_FIELDS = (
    "ref", "name", "kind", "options", "selected", "content_type", "content",
    "tags", "rationale", "ref_doc", "owner", "status",
)
# 変更すると承認をやり直す必要があるフィールド
_CONTENT_COLUMNS = ("options", "selected", "content_type", "content")

# action -> (許可される現在の状態, 次の状態, コメント必須)
TRANSITIONS: dict[str, tuple[tuple[str, ...], str, bool]] = {
    "request_review": (("draft",), "in_review", False),
    "approve": (("in_review",), "approved", False),
    "reject": (("in_review",), "draft", True),
    "withdraw": (("in_review",), "draft", False),
}


def item_snapshot(i: dict[str, Any]) -> dict[str, Any]:
    return {k: i.get(k) for k in ITEM_SNAPSHOT_FIELDS}


# ---------------------------------------------------------------- 検証

def _str_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise Invalid(f"{label}は文字列の配列で指定してください")
    return [x.strip() for x in value if x.strip()]


def normalize_options(value: Any) -> list[dict[str, Any]]:
    """選択肢の配列を正規化する。文字列だけの配列（キーの列挙）も受け付ける。"""
    if not isinstance(value, list):
        raise Invalid("選択肢は配列で指定してください")
    if len(value) > MAX_OPTIONS:
        raise Invalid(f"選択肢が多すぎます（最大 {MAX_OPTIONS} 件）")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for o in value:
        if isinstance(o, str):
            o = {"key": o}
        if not isinstance(o, dict):
            raise Invalid("選択肢はオブジェクト（key, label, description, pros, cons, attributes）で指定してください")
        key = o.get("key")
        if not isinstance(key, str) or not key.strip() or len(key) > 64:
            raise Invalid(f"選択肢のキーは 1〜64 文字の文字列で指定してください: {key!r}")
        key = key.strip()
        if key in seen:
            raise Invalid(f"選択肢のキーが重複しています: {key}")
        seen.add(key)
        for f in ("label", "description"):
            if o.get(f) is not None and not isinstance(o[f], str):
                raise Invalid(f"選択肢 {key} の {f} は文字列で指定してください")
        attrs = o.get("attributes") or {}
        if not isinstance(attrs, dict):
            raise Invalid(f"選択肢 {key} の attributes はオブジェクトで指定してください")
        out.append({
            "key": key,
            "label": (o.get("label") or key).strip(),
            "description": o.get("description") or "",
            "pros": _str_list(o.get("pros"), f"選択肢 {key} の pros "),
            "cons": _str_list(o.get("cons"), f"選択肢 {key} の cons "),
            "attributes": attrs,
        })
    return out


def _normalize_content(content_type: str, content: Any) -> Any:
    if content is None:
        return None
    if content_type == "json":
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError as e:
                raise Invalid(f"本文が JSON として解釈できません: {e}") from None
    elif not isinstance(content, str):
        raise Invalid(f"{content_type} 形式の本文は文字列で指定してください")
    if len(dumps(content).encode("utf-8")) > MAX_CONTENT_BYTES:
        raise Invalid(f"本文が大きすぎます（最大 {MAX_CONTENT_BYTES} バイト）")
    return content


def _item_fields(ctx: Ctx, system_id: int, data: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    """入力 dict を検証して DB 列の dict にする（current があれば部分更新）。"""
    cur = current or {}
    fields: dict[str, Any] = {}

    kind = data.get("kind", cur.get("kind"))
    if kind not in ITEM_KINDS:
        raise Invalid(f"種別は {' / '.join(ITEM_KINDS)} のいずれかで指定してください: {kind!r}")
    if current is not None and kind != cur["kind"]:
        raise Invalid("種別は変更できません。削除して作り直してください")
    if current is None:
        fields["kind"] = kind

    if "layer_id" in data or "layer" in data or current is None:
        layer = None
        if data.get("layer_id"):
            layer = repo.get_layer(ctx, int(data["layer_id"]))
        elif data.get("layer"):
            layer = next((l for l in repo.list_layers(ctx, system_id) if l["key"] == data["layer"]), None)
        if layer is None or layer["system_id"] != system_id:
            raise Invalid("レイヤーを layer_id またはレイヤーキー（layer）で指定してください")
        fields["layer_id"] = layer["id"]
    if "key" in data or current is None:
        fields["key"] = editing.validate_identifier(data.get("key"), "構成項目キー")
    if "name" in data or current is None:
        fields["name"] = editing.require_text(data, "name", "名前")

    if kind == "choice":
        options = cur.get("options", [])
        if "options" in data or current is None:
            options = normalize_options(data.get("options") or [])
            if not options:
                raise Invalid("選択肢項目には選択肢が 1 つ以上必要です")
            fields["options"] = dumps(options)
        if "selected" in data or "options" in data or current is None:
            selected = data.get("selected", cur.get("selected"))
            if selected == "":
                selected = None
            keys = [o["key"] for o in options]
            if selected is not None and selected not in keys:
                raise Invalid(f"採用する選択肢は {', '.join(keys)} のいずれかで指定してください: {selected!r}")
            fields["selected"] = selected
        if data.get("content") is not None:
            raise Invalid("選択肢項目に本文（content）は指定できません")
    else:
        ctype = data.get("content_type") or cur.get("content_type") or "text"
        if ctype not in CONTENT_TYPES:
            raise Invalid(f"本文の形式は {' / '.join(CONTENT_TYPES)} のいずれかで指定してください: {ctype!r}")
        if "content_type" in data or current is None:
            fields["content_type"] = ctype
        if "content" in data or "content_type" in data or current is None:
            fields["content"] = dumps(_normalize_content(ctype, data.get("content", cur.get("content"))))
        if data.get("options") or data.get("selected"):
            raise Invalid("不定形データに選択肢（options / selected）は指定できません")

    if "tags" in data or current is None:
        fields["tags"] = dumps(_str_list(data.get("tags"), "タグ"))
    for f in ("rationale", "ref_doc", "owner"):
        if f in data or current is None:
            v = data.get(f) or ""
            if not isinstance(v, str):
                raise Invalid(f"{f} は文字列で指定してください")
            fields[f] = v
    return fields


def _rules_using(ctx: Ctx, system_id: int, ref: str) -> list[str]:
    return [
        r["key"] for r in repo.list_rules(ctx, system_id)
        if ref in calc.rule_refs(repo.to_engine_rule(r))
    ]


# ---------------------------------------------------------------- CRUD

def create_item(ctx: Ctx, system_id: int, data: dict[str, Any], comment: str = "") -> dict[str, Any]:
    repo.get_system(ctx, system_id)
    fields = _item_fields(ctx, system_id, data, None)
    with transaction(ctx.conn):
        editing.check_ref_unique(ctx, system_id, fields["layer_id"], fields["key"])
        fields.update(system_id=system_id, updated_at=now())
        cols = ", ".join(fields)
        cur = ctx.conn.execute(
            f"INSERT INTO items ({cols}) VALUES ({', '.join('?' * len(fields))})", list(fields.values())
        )
        item = repo.get_item(ctx, cur.lastrowid)
        history.add_revision(ctx, system_id, "item", item["ref"], "create", after=item_snapshot(item), comment=comment)
        editing._touch_system(ctx, system_id)
    return item


def update_item(ctx: Ctx, item_id: int, data: dict[str, Any], comment: str = "") -> dict[str, Any]:
    before = repo.get_item(ctx, item_id)
    sid = before["system_id"]
    fields = _item_fields(ctx, sid, data, before)
    with transaction(ctx.conn):
        layer_id = fields.get("layer_id", before["layer_id"])
        key = fields.get("key", before["key"])
        if layer_id != before["layer_id"] or key != before["key"]:
            editing.check_ref_unique(ctx, sid, layer_id, key, exclude=("item", item_id))
        stored = {
            "options": dumps(before["options"]), "selected": before["selected"],
            "content_type": before["content_type"], "content": dumps(before["content"]),
        }
        content_changed = any(c in fields and fields[c] != stored[c] for c in _CONTENT_COLUMNS)
        selected_changed = "selected" in fields and fields["selected"] != before["selected"]
        if content_changed and before["status"] != "draft":
            fields["status"] = "draft"
        fields["updated_at"] = now()
        editing._update_row(ctx, "items", item_id, fields, data.get("row_version"))
        after = repo.get_item(ctx, item_id)
        if after["ref"] != before["ref"]:
            editing.rename_refs(ctx, sid, {before["ref"]: after["ref"]})
        history.add_revision(
            ctx, sid, "item", after["ref"], "update",
            before=item_snapshot(before), after=item_snapshot(after), comment=comment,
        )
        if content_changed and before["status"] != "draft":
            history.audit(
                ctx, sid, "item.revert_to_draft", "item", after["ref"],
                {"from": before["status"], "reason": "content_changed"},
            )
        if selected_changed:
            # 採用した選択肢を式で参照している下流のパラメータを要再計算にする
            editing.propagate_value_change(ctx, sid, [after["ref"]])
        editing._touch_system(ctx, sid)
    return repo.get_item(ctx, item_id)


def delete_item(ctx: Ctx, item_id: int, comment: str = "") -> None:
    item = repo.get_item(ctx, item_id)
    sid = item["system_id"]
    users = _rules_using(ctx, sid, item["ref"])
    if users:
        raise Invalid(f"構成項目 {item['ref']} はルールから参照されているため削除できません", {"rules": users})
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
        history.add_revision(ctx, sid, "item", item["ref"], "delete", before=item_snapshot(item), comment=comment)
        editing._touch_system(ctx, sid)


# ---------------------------------------------------------------- 状態遷移

def review_blockers(item: dict[str, Any]) -> list[str]:
    reasons = []
    if item["kind"] == "choice" and item["selected"] is None:
        reasons.append("採用する選択肢が未設定です")
    if item["kind"] == "document" and item["content"] in (None, ""):
        reasons.append("本文が未設定です")
    return reasons


def transition(ctx: Ctx, item_id: int, action: str, comment: str = "", row_version: int | None = None) -> dict[str, Any]:
    if action not in TRANSITIONS:
        raise Invalid(f"不明な操作です: {action}（{' / '.join(TRANSITIONS)}）")
    allowed, next_status, need_comment = TRANSITIONS[action]
    item = repo.get_item(ctx, item_id)
    if item["status"] not in allowed:
        raise Invalid(
            f"{STATUS_LABELS[item['status']]}の状態では「{action}」はできません",
            {"status": item["status"], "allowed_from": list(allowed)},
        )
    if need_comment and not (comment or "").strip():
        raise Invalid("この操作にはコメントが必要です")
    if action == "request_review":
        blockers = review_blockers(item)
        if blockers:
            raise Invalid("レビュー依頼できません", {"reasons": blockers})
    with transaction(ctx.conn):
        editing._update_row(ctx, "items", item_id, {"status": next_status, "updated_at": now()}, row_version)
        history.audit(
            ctx, item["system_id"], f"item.{action}", "item", item["ref"],
            {"from": item["status"], "to": next_status}, comment,
        )
    return repo.get_item(ctx, item_id)
