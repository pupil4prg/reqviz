"""変更履歴（revisions）と監査ログ（audit_log）。"""

from __future__ import annotations

from typing import Any

from ..context import Ctx
from ..db import dumps, loads, now
from ..errors import NotFound


def add_revision(
    ctx: Ctx,
    system_id: int,
    entity: str,
    entity_key: str,
    op: str,
    before: Any = None,
    after: Any = None,
    comment: str = "",
) -> None:
    ctx.conn.execute(
        "INSERT INTO revisions (system_id, entity, entity_key, op, before_json, after_json, actor, comment, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            system_id,
            entity,
            entity_key,
            op,
            None if before is None else dumps(before),
            None if after is None else dumps(after),
            ctx.actor,
            comment,
            now(),
        ),
    )


def list_revisions(
    ctx: Ctx, system_id: int, entity: str | None = None, entity_key: str | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM revisions WHERE system_id = ?"
    args: list[Any] = [system_id]
    if entity:
        sql += " AND entity = ?"
        args.append(entity)
    if entity_key:
        sql += " AND entity_key = ?"
        args.append(entity_key)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return [revision_dict(r) for r in ctx.conn.execute(sql, args)]


def revision_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["before"] = loads(d.pop("before_json"))
    d["after"] = loads(d.pop("after_json"))
    d["changes"] = diff_fields(d["before"], d["after"])
    return d


def get_revision(ctx: Ctx, revision_id: int) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM revisions WHERE id = ?", (revision_id,)).fetchone()
    if r is None:
        raise NotFound(f"リビジョンが見つかりません: {revision_id}")
    return revision_dict(r)


def diff_fields(before: Any, after: Any) -> list[dict[str, Any]]:
    """2 つの dict のフィールド単位の差分。"""
    if not isinstance(before, dict) or not isinstance(after, dict):
        return []
    keys = sorted(set(before) | set(after))
    return [
        {"field": k, "before": before.get(k), "after": after.get(k)}
        for k in keys
        if before.get(k) != after.get(k)
    ]


def audit(
    ctx: Ctx,
    system_id: int | None,
    action: str,
    target_type: str | None = None,
    target_key: str | None = None,
    detail: Any = None,
    comment: str = "",
) -> None:
    ctx.conn.execute(
        "INSERT INTO audit_log (system_id, actor, action, target_type, target_key, detail_json, comment, at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            system_id,
            ctx.actor,
            action,
            target_type,
            target_key,
            None if detail is None else dumps(detail),
            comment,
            now(),
        ),
    )


def list_audit(ctx: Ctx, system_id: int | None, target_key: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    """監査ログ。system_id が None なら全体（システム削除・テンプレート・設定の操作を含む）。"""
    sql = "SELECT * FROM audit_log WHERE 1 = 1"
    args: list[Any] = []
    if system_id is not None:
        sql += " AND system_id = ?"
        args.append(system_id)
    if target_key:
        sql += " AND target_key = ?"
        args.append(target_key)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in ctx.conn.execute(sql, args):
        d = dict(r)
        d["detail"] = loads(d.pop("detail_json"))
        out.append(d)
    return out
