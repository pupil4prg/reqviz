"""パラメータ単位の状態遷移。"""

from __future__ import annotations

from typing import Any

from ..context import Ctx
from ..db import now, transaction
from ..errors import Invalid
from ..repo import history
from ..repo import model as repo
from . import analysis
from .editing import _update_row

STATUS_LABELS = {
    "draft": "下書き",
    "ai_evaluated": "AI評価済み",
    "in_review": "レビュー中",
    "approved": "承認済み",
}

# action -> (許可される現在の状態, 次の状態, コメント必須)
TRANSITIONS: dict[str, tuple[tuple[str, ...], str | None, bool]] = {
    "ai_evaluate": (("draft",), "ai_evaluated", False),  # AI 評価ジョブ完了時に内部から呼ぶ
    "skip_ai": (("draft",), "ai_evaluated", False),
    "request_review": (("ai_evaluated",), "in_review", False),
    "approve": (("in_review",), "approved", False),
    "reject": (("in_review",), "draft", True),
    "withdraw": (("ai_evaluated", "in_review"), "draft", False),
    "ack_upstream": (("approved",), None, True),  # 状態は変えずにフラグだけ解除
}


def review_blockers(ctx: Ctx, param: dict[str, Any]) -> list[str]:
    """レビュー依頼できない理由の一覧。"""
    reasons = []
    if param["flag_stale"]:
        reasons.append("要再計算のフラグが残っています")
    if param["value"] is None:
        reasons.append("値が未設定です")
    for v in analysis.violations(ctx, param["system_id"]):
        if v.severity == "error" and param["ref"] in v.refs:
            reasons.append(f"error の制約違反に関与しています（{v.rule_key}）")
    return reasons


def transition(ctx: Ctx, param_id: int, action: str, comment: str = "", row_version: int | None = None) -> dict[str, Any]:
    if action not in TRANSITIONS:
        raise Invalid(f"不明な操作です: {action}")
    allowed, next_status, need_comment = TRANSITIONS[action]
    p = repo.get_param(ctx, param_id)
    sid = p["system_id"]
    if p["status"] not in allowed:
        raise Invalid(
            f"{STATUS_LABELS[p['status']]}の状態では「{action}」はできません",
            {"status": p["status"], "allowed_from": list(allowed)},
        )
    if need_comment and not (comment or "").strip():
        raise Invalid("この操作にはコメントが必要です")
    if action == "ack_upstream" and not p["flag_upstream_changed"]:
        raise Invalid("承認後の上流変更フラグは立っていません")
    if action == "request_review":
        blockers = review_blockers(ctx, p)
        if blockers:
            raise Invalid("レビュー依頼できません", {"reasons": blockers})

    fields: dict[str, Any] = {"updated_at": now()}
    if next_status:
        fields["status"] = next_status
    if action == "skip_ai":
        fields["ai_skipped"] = 1
    if action == "ai_evaluate":
        fields["ai_skipped"] = 0
    if action in ("approve", "ack_upstream"):
        fields["flag_upstream_changed"] = 0

    with transaction(ctx.conn):
        _update_row(ctx, "params", param_id, fields, row_version)
        history.audit(
            ctx, sid, f"param.{action}", "param", p["ref"],
            {"from": p["status"], "to": next_status or p["status"], "value": p["value"]},
            comment,
        )
    return repo.get_param(ctx, param_id)


def approval_queue(ctx: Ctx, system_id: int) -> dict[str, Any]:
    params = repo.list_params(ctx, system_id)
    in_review = [p for p in params if p["status"] == "in_review"]
    upstream = [p for p in params if p["flag_upstream_changed"]]
    baselines = [
        dict(r) for r in ctx.conn.execute(
            "SELECT id, version_no, label, created_by, created_at FROM baselines WHERE system_id = ? AND status = 'in_review' ORDER BY id",
            (system_id,),
        )
    ]
    return {"params": in_review, "upstream_changed": upstream, "baselines": baselines}
