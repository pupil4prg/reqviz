"""システム全体の版（ベースライン）の作成・承認・差し戻し・復元・差分。"""

from __future__ import annotations

from typing import Any

from ..context import Ctx
from ..db import dumps, loads, now, transaction
from ..engine import calc
from ..errors import Invalid, NotFound
from ..repo import history
from ..repo import model as repo
from . import analysis, snapshot


def _baseline_dict(r: Any, with_payload: bool = False) -> dict[str, Any]:
    d = dict(r)
    d["warnings"] = loads(d["warnings"], [])
    payload = d.pop("payload")
    if with_payload:
        d["payload"] = loads(payload)
    return d


def list_baselines(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    rows = ctx.conn.execute("SELECT * FROM baselines WHERE system_id = ? ORDER BY version_no DESC", (system_id,))
    return [_baseline_dict(r) for r in rows]


def get_baseline(ctx: Ctx, baseline_id: int, with_payload: bool = True) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM baselines WHERE id = ?", (baseline_id,)).fetchone()
    if r is None:
        raise NotFound(f"版が見つかりません: {baseline_id}")
    return _baseline_dict(r, with_payload)


def readiness(ctx: Ctx, system_id: int) -> dict[str, Any]:
    """版を作成（レビュー依頼）できるかの判定。blockers が空なら作成可能。"""
    params = repo.list_params(ctx, system_id)
    blockers: list[str] = []
    if not params:
        blockers.append("パラメータがありません")
    not_approved = [p["ref"] for p in params if p["status"] != "approved"]
    if not_approved:
        blockers.append(f"未承認のパラメータが {len(not_approved)} 件あります: {', '.join(not_approved[:10])}")
    stale = [p["ref"] for p in params if p["flag_stale"]]
    if stale:
        blockers.append(f"要再計算のパラメータがあります: {', '.join(stale[:10])}")
    upstream = [p["ref"] for p in params if p["flag_upstream_changed"]]
    if upstream:
        blockers.append(f"承認後に上流が変更されたパラメータがあります: {', '.join(upstream[:10])}")
    viols = analysis.violations(ctx, system_id)
    errors = [v for v in viols if v.severity == "error"]
    for v in errors:
        blockers.append(f"error の制約違反: {v.rule_key} — {v.message}")
    rule_errs = calc.validate_rules(*repo.load_engine_model(ctx, system_id))
    for key, msgs in rule_errs.items():
        blockers.append(f"ルール {key} に誤りがあります: {'; '.join(msgs)}")
    if ctx.conn.execute(
        "SELECT 1 FROM baselines WHERE system_id = ? AND status = 'in_review'", (system_id,)
    ).fetchone():
        blockers.append("レビュー中の版があります。承認または差し戻ししてから作成してください")
    warnings = [v.to_dict() for v in viols if v.severity == "warning"]
    return {"ready": not blockers, "blockers": blockers, "warnings": warnings}


def create_baseline(ctx: Ctx, system_id: int, label: str = "") -> dict[str, Any]:
    repo.get_system(ctx, system_id)
    with transaction(ctx.conn):
        state = readiness(ctx, system_id)
        if not state["ready"]:
            raise Invalid("版を作成できません", {"reasons": state["blockers"]})
        version_no = ctx.conn.execute(
            "SELECT COALESCE(MAX(version_no), 0) + 1 FROM baselines WHERE system_id = ?", (system_id,)
        ).fetchone()[0]
        payload = snapshot.export_payload(ctx, system_id)
        cur = ctx.conn.execute(
            "INSERT INTO baselines (system_id, version_no, label, status, payload, warnings, created_by, created_at)"
            " VALUES (?, ?, ?, 'in_review', ?, ?, ?, ?)",
            (system_id, version_no, label or "", dumps(payload), dumps(state["warnings"]), ctx.actor, now()),
        )
        history.audit(ctx, system_id, "baseline.create", "baseline", f"v{version_no}", {"warnings": len(state["warnings"])}, label)
    return get_baseline(ctx, cur.lastrowid, with_payload=False)


def _decide(ctx: Ctx, baseline_id: int, approve: bool, comment: str) -> dict[str, Any]:
    b = get_baseline(ctx, baseline_id, with_payload=False)
    if b["status"] != "in_review":
        raise Invalid("レビュー中の版ではありません")
    comment = (comment or "").strip()
    if not approve and not comment:
        raise Invalid("差し戻しにはコメントが必要です")
    if approve and b["warnings"] and not comment:
        raise Invalid(
            f"warning の制約違反が {len(b['warnings'])} 件あります。承認するにはコメントを入力してください",
            {"warnings": b["warnings"]},
        )
    status = "approved" if approve else "rejected"
    with transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE baselines SET status = ?, decided_by = ?, decided_at = ?, decision_comment = ? WHERE id = ?",
            (status, ctx.actor, now(), comment, baseline_id),
        )
        if approve:
            ctx.conn.execute("UPDATE systems SET current_baseline_id = ? WHERE id = ?", (baseline_id, b["system_id"]))
        history.audit(ctx, b["system_id"], f"baseline.{'approve' if approve else 'reject'}", "baseline", f"v{b['version_no']}", None, comment)
    return get_baseline(ctx, baseline_id, with_payload=False)


def approve_baseline(ctx: Ctx, baseline_id: int, comment: str = "") -> dict[str, Any]:
    return _decide(ctx, baseline_id, True, comment)


def reject_baseline(ctx: Ctx, baseline_id: int, comment: str = "") -> dict[str, Any]:
    return _decide(ctx, baseline_id, False, comment)


def restore_baseline(ctx: Ctx, baseline_id: int, comment: str = "") -> None:
    b = get_baseline(ctx, baseline_id)
    with transaction(ctx.conn):
        snapshot.replace_with_payload(
            ctx, b["system_id"], b["payload"], keep_status=True, op="restore",
            comment=comment or f"版 v{b['version_no']} から復元",
        )
        history.audit(ctx, b["system_id"], "baseline.restore", "baseline", f"v{b['version_no']}", None, comment)


def resolve_payload(ctx: Ctx, system_id: int, ref: str | None) -> dict[str, Any]:
    """'working' / 'current' / 版 ID から比較用のスナップショットを得る。"""
    if ref in (None, "", "working"):
        return snapshot.export_payload(ctx, system_id)
    if ref == "current":
        s = repo.get_system(ctx, system_id)
        if not s["current_baseline_id"]:
            raise Invalid("承認済みの版がありません")
        ref = str(s["current_baseline_id"])
    try:
        bid = int(ref)
    except ValueError:
        raise Invalid(f"比較対象が不正です: {ref}") from None
    b = get_baseline(ctx, bid)
    if b["system_id"] != system_id:
        raise Invalid("別のシステムの版です")
    return b["payload"]


def diff(ctx: Ctx, system_id: int, from_ref: str | None, to_ref: str | None) -> dict[str, Any]:
    a = resolve_payload(ctx, system_id, from_ref)
    b = resolve_payload(ctx, system_id, to_ref)
    return snapshot.diff_payloads(a, b)


def changed_since_current(ctx: Ctx, system_id: int) -> list[str] | None:
    """現行ベースラインから変更されたパラメータ参照（承認済みの版がなければ None）。"""
    s = repo.get_system(ctx, system_id)
    if not s["current_baseline_id"]:
        return None
    d = diff(ctx, system_id, "current", "working")
    return [x["key"] for x in d["params"]]
