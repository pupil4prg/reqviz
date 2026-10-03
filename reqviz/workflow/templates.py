"""テンプレート（ひな形）の管理。作成時にコピーし、更新は既存システムに反映しない。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..context import Ctx
from ..db import dumps, loads, now, transaction
from ..errors import Invalid, NotFound
from ..repo import history
from ..repo import model as repo
from . import editing, snapshot


def _tpl_dict(r: Any, with_payload: bool = False) -> dict[str, Any]:
    d = dict(r)
    payload = loads(d.pop("payload"))
    d["summary"] = snapshot._summary(payload)
    if with_payload:
        d["payload"] = payload
    return d


def list_templates(ctx: Ctx) -> list[dict[str, Any]]:
    rows = ctx.conn.execute("SELECT * FROM templates ORDER BY name, version DESC")
    return [_tpl_dict(r) for r in rows]


def get_template(ctx: Ctx, template_id: int, with_payload: bool = True) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM templates WHERE id = ?", (template_id,)).fetchone()
    if r is None:
        raise NotFound(f"テンプレートが見つかりません: {template_id}")
    return _tpl_dict(r, with_payload)


def _strip_status(payload: dict[str, Any]) -> dict[str, Any]:
    p = json.loads(json.dumps(payload))
    for x in p.get("params", []):
        x.pop("status", None)
        x.pop("ai_skipped", None)
    return p


def save_template(ctx: Ctx, name: str, description: str, payload: dict[str, Any]) -> dict[str, Any]:
    """同名テンプレートがあれば新しいバージョンとして保存する。"""
    name = (name or "").strip()
    if not name:
        raise Invalid("テンプレート名は必須です")
    if payload.get("format") != snapshot.FORMAT:
        raise Invalid("テンプレートの形式が不正です")
    payload = _strip_status(payload)
    with transaction(ctx.conn):
        version = ctx.conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM templates WHERE name = ?", (name,)
        ).fetchone()[0]
        cur = ctx.conn.execute(
            "INSERT INTO templates (name, version, description, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, version, description or "", dumps(payload), now()),
        )
        history.audit(ctx, None, "template.save", "template", f"{name} v{version}")
    return get_template(ctx, cur.lastrowid, with_payload=False)


def template_from_system(ctx: Ctx, system_id: int, name: str, description: str = "") -> dict[str, Any]:
    payload = snapshot.export_payload(ctx, system_id, include_status=False)
    return save_template(ctx, name, description, payload)


def import_template(ctx: Ctx, payload: dict[str, Any], name: str | None = None, description: str | None = None) -> dict[str, Any]:
    """JSON（reqviz/system 形式）をテンプレートとして取り込む。内容は一時システムで検証する。"""
    _validate_payload(ctx, payload)
    sysinfo = payload.get("system", {})
    return save_template(ctx, name or sysinfo.get("name") or "", description if description is not None else sysinfo.get("description", ""), payload)


def _validate_payload(ctx: Ctx, payload: dict[str, Any]) -> None:
    """一時システムに展開して検証し、ロールバックする。"""
    outer = ctx.conn.in_transaction
    ctx.conn.execute("SAVEPOINT tpl_check" if outer else "BEGIN")
    try:
        ts = now()
        cur = ctx.conn.execute(
            "INSERT INTO systems (name, description, created_at, updated_at) VALUES ('_check', '', ?, ?)", (ts, ts)
        )
        snapshot.replace_with_payload(ctx, cur.lastrowid, payload, keep_status=False, op="create")
    finally:
        if outer:
            ctx.conn.execute("ROLLBACK TO tpl_check")
            ctx.conn.execute("RELEASE tpl_check")
        else:
            ctx.conn.execute("ROLLBACK")


def delete_template(ctx: Ctx, template_id: int) -> None:
    t = get_template(ctx, template_id, with_payload=False)
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM templates WHERE id = ?", (template_id,))
        history.audit(ctx, None, "template.delete", "template", f"{t['name']} v{t['version']}")


def create_system_from_template(ctx: Ctx, template_id: int, name: str, description: str = "") -> dict[str, Any]:
    t = get_template(ctx, template_id)
    with transaction(ctx.conn):
        s = editing.create_system(ctx, {"name": name, "description": description or t["description"]})
        snapshot.replace_with_payload(ctx, s["id"], t["payload"], keep_status=False, op="create", comment=f"テンプレート {t['name']} v{t['version']} から作成")
        ctx.conn.execute(
            "UPDATE systems SET template_id = ?, template_version = ? WHERE id = ?", (t["id"], t["version"], s["id"])
        )
        _mark_formula_targets_stale(ctx, s["id"])
    return repo.get_system(ctx, s["id"])


def clone_system(ctx: Ctx, system_id: int, name: str) -> dict[str, Any]:
    src = repo.get_system(ctx, system_id)
    payload = snapshot.export_payload(ctx, system_id)
    with transaction(ctx.conn):
        s = editing.create_system(ctx, {"name": name or f"{src['name']} のコピー", "description": src["description"]})
        snapshot.replace_with_payload(ctx, s["id"], payload, keep_status=True, op="create", comment=f"{src['name']} から複製")
        _copy_flags(ctx, system_id, s["id"])
    return repo.get_system(ctx, s["id"])


def _copy_flags(ctx: Ctx, src_id: int, dst_id: int) -> None:
    src = repo.params_by_ref(ctx, src_id)
    for ref, p in repo.params_by_ref(ctx, dst_id).items():
        if ref in src:
            s = src[ref]
            ctx.conn.execute(
                "UPDATE params SET flag_stale = ?, flag_upstream_changed = ? WHERE id = ?",
                (int(s["flag_stale"]), int(s["flag_upstream_changed"]), p["id"]),
            )


def _mark_formula_targets_stale(ctx: Ctx, system_id: int) -> None:
    """テンプレートから作成した直後は、算出式の代入先をすべて要再計算にする。"""
    by_ref = repo.params_by_ref(ctx, system_id)
    for r in repo.list_rules(ctx, system_id):
        if r["kind"] == "formula" and r["enabled"] and r["target"] in by_ref:
            ctx.conn.execute("UPDATE params SET flag_stale = 1 WHERE id = ?", (by_ref[r["target"]]["id"],))


def seed_bundled(ctx: Ctx, directory: Path) -> list[str]:
    """同梱テンプレート（templates/*.json）のうち未登録のものを登録する。"""
    added = []
    for f in sorted(directory.glob("*.json")):
        payload = json.loads(f.read_text(encoding="utf-8"))
        name = payload.get("system", {}).get("name") or f.stem
        if ctx.conn.execute("SELECT 1 FROM templates WHERE name = ?", (name,)).fetchone():
            continue
        import_template(ctx, payload)
        added.append(name)
    return added
