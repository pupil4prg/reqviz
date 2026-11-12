"""タグの集計・一括付け外し・名前変更と、パラメータと構成項目を横断する検索。

タグは自由な文字列のラベルで、「開発中」「要確認」のような作業状況や、分類に使う。
承認の状態（下書き → 承認済み）とは独立していて、タグを変えても承認状態は変わらない。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..context import Ctx
from ..db import transaction
from ..errors import Invalid
from ..repo import history
from ..repo import model as repo
from . import editing, items

PARAM_STATUSES = ("draft", "ai_evaluated", "in_review", "approved")


def tag_counts(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    """システムで使われているタグと、付いている件数。"""
    repo.get_system(ctx, system_id)
    counts: dict[str, dict[str, Any]] = {}
    for kind, rows in (("params", repo.list_params(ctx, system_id)), ("items", repo.list_items(ctx, system_id))):
        for row in rows:
            for tag in row["tags"]:
                c = counts.setdefault(tag, {"tag": tag, "params": 0, "items": 0})
                c[kind] += 1
    return sorted(counts.values(), key=lambda c: c["tag"].casefold())


def _set_tags(ctx: Ctx, kind: str, row: dict[str, Any], tags: list[str], comment: str) -> None:
    if kind == "param":
        editing.update_param(ctx, row["id"], {"tags": tags}, comment=comment)
    else:
        items.update_item(ctx, row["id"], {"tags": tags}, comment=comment)


def apply_tags(
    ctx: Ctx, system_id: int, refs: Iterable[str], add: Any = None, remove: Any = None, comment: str = ""
) -> dict[str, Any]:
    """複数のパラメータ・構成項目に、まとめてタグを付け外しする。変更は 1 件ずつ履歴に残る。"""
    add_tags, remove_tags = editing.normalize_tags(add), {t.casefold() for t in editing.normalize_tags(remove)}
    if not add_tags and not remove_tags:
        raise Invalid("付けるタグ（add）か外すタグ（remove）を指定してください")
    targets = {**{r: ("item", i) for r, i in repo.items_by_ref(ctx, system_id).items()},
               **{r: ("param", p) for r, p in repo.params_by_ref(ctx, system_id).items()}}
    refs = list(dict.fromkeys(refs))
    unknown = [r for r in refs if r not in targets]
    if unknown:
        raise Invalid(f"存在しない参照名です: {', '.join(unknown[:10])}")
    updated = []
    with transaction(ctx.conn):
        for ref in refs:
            kind, row = targets[ref]
            kept = [t for t in row["tags"] if t.casefold() not in remove_tags]
            new = kept + [t for t in add_tags if t.casefold() not in {k.casefold() for k in kept}]
            if new != row["tags"]:
                _set_tags(ctx, kind, row, editing.normalize_tags(new), comment)
                updated.append(ref)
        history.audit(
            ctx, system_id, "tags.apply", "system", None,
            {"add": add_tags, "remove": sorted(remove_tags), "count": len(updated)}, comment,
        )
    return {"updated": updated, "unchanged": [r for r in refs if r not in updated]}


def rename_tag(ctx: Ctx, system_id: int, old: Any, new: Any, comment: str = "") -> dict[str, Any]:
    """タグの名前を、システム全体でまとめて変える（new が既に付いている項目では統合される）。"""
    old_tags, new_tags = editing.normalize_tags(old), editing.normalize_tags(new)
    if len(old_tags) != 1 or len(new_tags) != 1:
        raise Invalid("変更前（from）と変更後（to）のタグを 1 つずつ指定してください")
    want = old_tags[0].casefold()
    refs = [
        row["ref"] for row in repo.list_params(ctx, system_id) + repo.list_items(ctx, system_id)
        if any(t.casefold() == want for t in row["tags"])
    ]
    if not refs:
        raise Invalid(f"タグ {old_tags[0]} は使われていません")
    return apply_tags(ctx, system_id, refs, add=new_tags, remove=old_tags, comment=comment or f"タグ名の変更: {old_tags[0]} → {new_tags[0]}")


# ---------------------------------------------------------------- 検索

def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _param_fields(p: dict[str, Any]) -> list[tuple[str, str]]:
    return [
        ("ref", p["ref"]), ("name", p["name"]), *[("alias", a["value"]) for a in p["aliases"]],
        ("value", _text(p["value"])), ("unit", p["unit"]), ("rationale", p["rationale"]), ("ref_doc", p["ref_doc"]),
        ("owner", p["owner"]), *[("tag", t) for t in p["tags"]],
    ]


def _item_fields(i: dict[str, Any]) -> list[tuple[str, str]]:
    fields = [
        ("ref", i["ref"]), ("name", i["name"]), ("rationale", i["rationale"]), ("ref_doc", i["ref_doc"]),
        ("owner", i["owner"]), *[("tag", t) for t in i["tags"]],
    ]
    if i["kind"] == "choice":
        fields.append(("selected", i["selected"] or ""))
        for o in i["options"]:
            fields += [("option", o["key"]), ("option", o["label"]), ("option", o["description"])]
    else:
        fields.append(("content", _text(i["content"])))
    return fields


def search(
    ctx: Ctx,
    system_id: int,
    q: str = "",
    tags: Any = None,
    tag_mode: str = "all",
    kind: str = "",
    status: str = "",
    layer: str = "",
    limit: int = 200,
) -> dict[str, Any]:
    """パラメータと構成項目を横断して検索する。

    - ``q``: 空白区切りの語をすべて含むもの（参照名・名前・別名・値・根拠・出典・担当・タグ・選択肢・本文が対象。大文字小文字は区別しない）
    - ``tags``: タグでの絞り込み。``tag_mode`` が ``all`` ならすべて、``any`` ならいずれかが付いているもの
    - ``kind``: ``param`` / ``item`` / ``choice`` / ``document``
    - ``status``: 承認の状態、または ``stale``（要再計算）
    """
    repo.get_system(ctx, system_id)
    if tag_mode not in ("all", "any"):
        raise Invalid("tag_mode は all / any のいずれかで指定してください")
    if kind not in ("", "param", "item", "choice", "document"):
        raise Invalid("kind は param / item / choice / document のいずれかで指定してください")
    if status not in ("", "stale", *PARAM_STATUSES):
        raise Invalid(f"status は {' / '.join(PARAM_STATUSES)} / stale のいずれかで指定してください")
    words = [w.casefold() for w in (q or "").split()]
    want_tags = [t.casefold() for t in editing.normalize_tags(tags)]

    def accept(row: dict[str, Any], fields: list[tuple[str, str]]) -> list[str] | None:
        if layer and row["layer_key"] != layer:
            return None
        if status == "stale":
            if not row.get("flag_stale"):
                return None
        elif status and row["status"] != status:
            return None
        have = {t.casefold() for t in row["tags"]}
        if want_tags and not (all if tag_mode == "all" else any)(t in have for t in want_tags):
            return None
        matched: list[str] = []
        for w in words:
            hit = [name for name, text in fields if text and w in text.casefold()]
            if not hit:
                return None
            matched += hit
        return list(dict.fromkeys(matched))

    results: list[dict[str, Any]] = []
    if kind in ("", "param"):
        for p in repo.list_params(ctx, system_id):
            m = accept(p, _param_fields(p))
            if m is not None:
                results.append({
                    "type": "param", "id": p["id"], "ref": p["ref"], "name": p["name"], "layer_key": p["layer_key"],
                    "aliases": p["aliases"], "tags": p["tags"], "status": p["status"], "flag_stale": p["flag_stale"],
                    "summary": _text(p["value"]) + (f" {p['unit']}" if p["unit"] and p["value"] is not None else ""),
                    "matched": m,
                })
    if kind in ("", "item", "choice", "document"):
        for i in repo.list_items(ctx, system_id, kind if kind in ("choice", "document") else None):
            m = accept(i, _item_fields(i))
            if m is not None:
                summary = (i["selected"] or "（未決定）") if i["kind"] == "choice" else _text(i["content"])[:80]
                results.append({
                    "type": i["kind"], "id": i["id"], "ref": i["ref"], "name": i["name"], "layer_key": i["layer_key"],
                    "aliases": [], "tags": i["tags"], "status": i["status"], "flag_stale": False,
                    "summary": summary, "matched": m,
                })
    return {"total": len(results), "results": results[: max(1, min(int(limit), 1000))]}
