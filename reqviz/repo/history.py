"""変更履歴（revisions）と監査ログ（audit_log）。"""

from __future__ import annotations

from typing import Any

from ..context import Ctx
from ..db import dumps, loads, now


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
    out = []
    for r in ctx.conn.execute(sql, args):
        d = dict(r)
        d["before"] = loads(d.pop("before_json"))
        d["after"] = loads(d.pop("after_json"))
        d["changes"] = diff_fields(d["before"], d["after"])
        out.append(d)
    return out


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


def list_audit(ctx: Ctx, system_id: int, target_key: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    sql = "SELECT * FROM audit_log WHERE system_id = ?"
    args: list[Any] = [system_id]
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
