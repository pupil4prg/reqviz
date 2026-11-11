"""REST API のルーティング定義。ハンドラは薄く保ち、処理は workflow / ai / io に委ねる。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .. import auth
from ..ai import jobs
from ..ai.prompts import PURPOSE_LABELS
from ..errors import BadRequest, Invalid
from ..io import export
from ..repo import history
from ..repo import model as repo
from ..router import Request, Response, Router
from ..errors import NotFound
from ..workflow import analysis, baseline, editing, items, param_state, snapshot, templates, versions

router = Router()
r = router.route


def _int_list(v: Any, label: str) -> list[int]:
    if not isinstance(v, list) or not all(isinstance(x, int) for x in v):
        raise BadRequest(f"{label} は整数の配列で指定してください")
    return v


def _str_list(v: Any, label: str) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise BadRequest(f"{label} は文字列の配列で指定してください")
    return v


def _download(body: bytes, content_type: str, filename: str) -> Response:
    return Response(body=body, content_type=content_type, filename=filename)


def _json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _require(req: Request, *roles: str) -> None:
    """ロールの確認。認証が無効なら何もしない。管理者は常に通す。"""
    if not req.app.config.auth_enabled:
        return
    p = req.principal
    if p is None or (p.role != "admin" and p.role not in roles):
        need = "・".join(auth.ROLE_LABELS[r] for r in (roles or ("admin",)))
        raise auth.Forbidden(f"この操作には{need}の権限が必要です")


_APPROVER_ACTIONS = ("approve", "reject", "ack_upstream")


def _json(obj: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), headers=headers or {})


# ---------------------------------------------------------------- 認証・アカウント

@r("GET", "/api/auth/me")
def auth_me(req: Request) -> Any:
    """ログイン状態（未ログインなら user は null）と CSRF トークン"""
    p = req.principal
    return {
        "auth_enabled": req.app.config.auth_enabled,
        "user": p.public() if p else None,
        "csrf_token": p.csrf_token if p else None,
        "roles": auth.ROLE_LABELS,
    }


@r("POST", "/api/auth/login")
def auth_login(req: Request) -> Any:
    """ID とパスワードでログインし、セッション Cookie を発行する"""
    cfg = req.app.config
    if not cfg.auth_enabled:
        raise BadRequest("ログイン認証は無効です")
    data = req.json()
    token, p = auth.login(req.ctx, data.get("login"), data.get("password"), cfg.session_hours)
    return _json(
        {"user": p.public(), "csrf_token": p.csrf_token},
        {"Set-Cookie": auth.session_cookie(token, cfg.session_hours, cfg.cookie_secure)},
    )


@r("POST", "/api/auth/logout")
def auth_logout(req: Request) -> Any:
    """ログアウトする"""
    auth.logout(req.ctx, req.principal)
    return _json({"ok": True}, {"Set-Cookie": auth.clear_cookie(req.app.config.cookie_secure)})


def _self(req: Request) -> auth.Principal:
    if req.principal is None or req.principal.user_id is None:
        raise BadRequest("ログイン認証が無効のため、この操作は使えません")
    return req.principal


@r("POST", "/api/auth/password")
def auth_password(req: Request) -> Any:
    """自分のパスワードを変更する（current_password, new_password）"""
    data = req.json()
    auth.change_password(req.ctx, _self(req), data.get("current_password"), data.get("new_password"))
    return {"ok": True}


@r("GET", "/api/auth/tokens")
def auth_tokens(req: Request) -> Any:
    """自分の API トークンの一覧（REST API・MCP・A2A 用）"""
    return auth.list_tokens(req.ctx, _self(req).user_id)  # type: ignore[arg-type]


@r("POST", "/api/auth/tokens")
def auth_token_create(req: Request) -> Any:
    """API トークンを発行する（平文は発行時にだけ返す）"""
    return auth.create_token(req.ctx, _self(req), req.json().get("name"))


@r("DELETE", "/api/auth/tokens/{id:int}")
def auth_token_delete(req: Request) -> Any:
    """API トークンを失効させる"""
    auth.delete_token(req.ctx, _self(req), req.params["id"])
    return {"ok": True}


@r("GET", "/api/users")
def users_list(req: Request) -> Any:
    """アカウントの一覧（管理者）"""
    _require(req)
    return auth.list_users(req.ctx)


@r("POST", "/api/users")
def users_create(req: Request) -> Any:
    """アカウントを作成する（管理者。login, password, role, display_name）"""
    _require(req)
    return auth.create_user(req.ctx, req.json())


@r("PUT", "/api/users/{id:int}")
def users_update(req: Request) -> Any:
    """アカウントを更新する（管理者。display_name, role, disabled, password の再設定）"""
    _require(req)
    return auth.update_user(req.ctx, req.params["id"], req.json(), req.principal)


@r("DELETE", "/api/users/{id:int}")
def users_delete(req: Request) -> Any:
    """アカウントを削除する（管理者）"""
    _require(req)
    auth.delete_user(req.ctx, req.params["id"], req.principal)
    return {"ok": True}


# ---------------------------------------------------------------- メタ

@r("GET", "/api")
def api_catalog(req: Request) -> Any:
    """API の一覧（メソッド・パス・説明）と、エージェント向けエンドポイント"""
    return {
        "name": "reqviz",
        "version": req.app.version,
        "routes": router.catalog(),
        "agent_endpoints": {"mcp": "/mcp", "a2a": "/a2a", "a2a_agent_card": "/.well-known/agent-card.json"},
    }


@r("GET", "/api/meta")
def meta(req: Request) -> Any:
    return {
        "user": req.ctx.actor,
        "role": req.principal.role if req.principal else None,
        "version": req.app.version,
        "status_labels": param_state.STATUS_LABELS,
        "item_status_labels": items.STATUS_LABELS,
        "item_kinds": items.KIND_LABELS,
        "ai_purposes": PURPOSE_LABELS,
        "llm": jobs.get_settings(req.ctx, req.app.config.llm),
    }


# ---------------------------------------------------------------- システム

@r("GET", "/api/systems")
def systems_list(req: Request) -> Any:
    return repo.list_systems(req.ctx)


@r("POST", "/api/systems")
def systems_create(req: Request) -> Any:
    data = req.json()
    if data.get("template_id"):
        return templates.create_system_from_template(req.ctx, int(data["template_id"]), data.get("name") or "", data.get("description") or "")
    return editing.create_system(req.ctx, data)


@r("GET", "/api/systems/{sid:int}")
def systems_get(req: Request) -> Any:
    return repo.get_system(req.ctx, req.params["sid"])


@r("PUT", "/api/systems/{sid:int}")
def systems_update(req: Request) -> Any:
    return editing.update_system(req.ctx, req.params["sid"], req.json())


@r("DELETE", "/api/systems/{sid:int}")
def systems_delete(req: Request) -> Any:
    editing.delete_system(req.ctx, req.params["sid"])
    return {"ok": True}


@r("POST", "/api/systems/{sid:int}/clone")
def systems_clone(req: Request) -> Any:
    return templates.clone_system(req.ctx, req.params["sid"], req.json().get("name") or "")


# ---------------------------------------------------------------- レイヤー

@r("GET", "/api/systems/{sid:int}/layers")
def layers_list(req: Request) -> Any:
    return repo.list_layers(req.ctx, req.params["sid"])


@r("POST", "/api/systems/{sid:int}/layers")
def layers_create(req: Request) -> Any:
    return editing.create_layer(req.ctx, req.params["sid"], req.json())


@r("POST", "/api/systems/{sid:int}/layers/reorder")
def layers_reorder(req: Request) -> Any:
    return editing.reorder_layers(req.ctx, req.params["sid"], _int_list(req.json().get("layer_ids"), "layer_ids"))


@r("GET", "/api/layers/{id:int}")
def layers_get(req: Request) -> Any:
    return repo.get_layer(req.ctx, req.params["id"])


@r("PUT", "/api/layers/{id:int}")
def layers_update(req: Request) -> Any:
    return editing.update_layer(req.ctx, req.params["id"], req.json())


@r("DELETE", "/api/layers/{id:int}")
def layers_delete(req: Request) -> Any:
    editing.delete_layer(req.ctx, req.params["id"])
    return {"ok": True}


# ---------------------------------------------------------------- パラメータ

@r("GET", "/api/systems/{sid:int}/params")
def params_list(req: Request) -> Any:
    return repo.list_params(req.ctx, req.params["sid"])


@r("POST", "/api/systems/{sid:int}/params")
def params_create(req: Request) -> Any:
    return editing.create_param(req.ctx, req.params["sid"], req.json())


@r("GET", "/api/systems/{sid:int}/params/by-ref/{ref}")
def params_get_by_ref(req: Request) -> Any:
    """参照名（layer.key）でパラメータを取得する"""
    p = repo.params_by_ref(req.ctx, req.params["sid"]).get(req.params["ref"])
    if p is None:
        raise NotFound(f"パラメータが見つかりません: {req.params['ref']}")
    return _param_detail(req, p)


@r("GET", "/api/params/{id:int}")
def params_get(req: Request) -> Any:
    return _param_detail(req, repo.get_param(req.ctx, req.params["id"]))


def _param_detail(req: Request, p: dict[str, Any]) -> Any:
    p["revisions"] = history.list_revisions(req.ctx, p["system_id"], "param", p["ref"], limit=50)
    p["audit"] = history.list_audit(req.ctx, p["system_id"], p["ref"], limit=50)
    p["review_blockers"] = param_state.review_blockers(req.ctx, p)
    return p


@r("PUT", "/api/params/{id:int}")
def params_update(req: Request) -> Any:
    data = req.json()
    return editing.update_param(req.ctx, req.params["id"], data, comment=data.get("comment") or "")


@r("DELETE", "/api/params/{id:int}")
def params_delete(req: Request) -> Any:
    editing.delete_param(req.ctx, req.params["id"])
    return {"ok": True}


@r("POST", "/api/params/{id:int}/transition")
def params_transition(req: Request) -> Any:
    data = req.json()
    if data.get("action") in _APPROVER_ACTIONS:
        _require(req, "approver")
    return param_state.transition(req.ctx, req.params["id"], data.get("action") or "", data.get("comment") or "", data.get("row_version"))


@r("POST", "/api/systems/{sid:int}/transition")
def params_bulk_transition(req: Request) -> Any:
    """複数パラメータへの一括操作。失敗したものは理由を返す。"""
    data = req.json()
    if data.get("action") in _APPROVER_ACTIONS:
        _require(req, "approver")
    ok, failed = [], []
    for pid in _int_list(data.get("param_ids"), "param_ids"):
        try:
            ok.append(param_state.transition(req.ctx, pid, data.get("action") or "", data.get("comment") or ""))
        except Invalid as e:
            failed.append({"id": pid, "message": e.message, "details": e.details})
    return {"ok": ok, "failed": failed}


# ---------------------------------------------------------------- 構成項目（選択肢・不定形データ）

@r("GET", "/api/systems/{sid:int}/items")
def items_list(req: Request) -> Any:
    """構成項目の一覧（kind=choice|document で絞り込み）"""
    return repo.list_items(req.ctx, req.params["sid"], req.query.get("kind") or None)


@r("POST", "/api/systems/{sid:int}/items")
def items_create(req: Request) -> Any:
    """構成項目を作成する（kind: choice は options/selected、document は content_type/content）"""
    data = req.json()
    return items.create_item(req.ctx, req.params["sid"], data, comment=data.get("comment") or "")


@r("GET", "/api/systems/{sid:int}/items/by-ref/{ref}")
def items_get_by_ref(req: Request) -> Any:
    """参照名（layer.key）で構成項目を取得する"""
    i = repo.items_by_ref(req.ctx, req.params["sid"]).get(req.params["ref"])
    if i is None:
        raise NotFound(f"構成項目が見つかりません: {req.params['ref']}")
    return _item_detail(req, i)


@r("GET", "/api/items/{id:int}")
def items_get(req: Request) -> Any:
    """構成項目の詳細（変更履歴・監査ログつき）"""
    return _item_detail(req, repo.get_item(req.ctx, req.params["id"]))


def _item_detail(req: Request, i: dict[str, Any]) -> Any:
    i["revisions"] = versions.timeline(req.ctx, i["system_id"], "item", i["ref"])[:50]
    i["audit"] = history.list_audit(req.ctx, i["system_id"], i["ref"], limit=50)
    i["review_blockers"] = items.review_blockers(i)
    return i


@r("PUT", "/api/items/{id:int}")
def items_update(req: Request) -> Any:
    """構成項目を更新する（内容を変えると下書きに戻る）"""
    data = req.json()
    return items.update_item(req.ctx, req.params["id"], data, comment=data.get("comment") or "")


@r("DELETE", "/api/items/{id:int}")
def items_delete(req: Request) -> Any:
    items.delete_item(req.ctx, req.params["id"])
    return {"ok": True}


@r("POST", "/api/items/{id:int}/transition")
def items_transition(req: Request) -> Any:
    """構成項目の状態遷移（request_review / approve / reject / withdraw）"""
    data = req.json()
    if data.get("action") in _APPROVER_ACTIONS:
        _require(req, "approver")
    return items.transition(req.ctx, req.params["id"], data.get("action") or "", data.get("comment") or "", data.get("row_version"))


# ---------------------------------------------------------------- 関連

@r("GET", "/api/systems/{sid:int}/relations")
def relations_list(req: Request) -> Any:
    return repo.list_relations(req.ctx, req.params["sid"])


@r("POST", "/api/systems/{sid:int}/relations")
def relations_create(req: Request) -> Any:
    return editing.create_relation(req.ctx, req.params["sid"], req.json())


@r("GET", "/api/relations/{id:int}")
def relations_get(req: Request) -> Any:
    return editing.get_relation(req.ctx, req.params["id"])


@r("PUT", "/api/relations/{id:int}")
def relations_update(req: Request) -> Any:
    """関連のメモを更新する"""
    return editing.update_relation(req.ctx, req.params["id"], req.json())


@r("DELETE", "/api/relations/{id:int}")
def relations_delete(req: Request) -> Any:
    editing.delete_relation(req.ctx, req.params["id"])
    return {"ok": True}


# ---------------------------------------------------------------- ルール

@r("GET", "/api/systems/{sid:int}/rules")
def rules_list(req: Request) -> Any:
    sid = req.params["sid"]
    errs = analysis.graph(req.ctx, sid)["rule_errors"]
    return [{**x, "errors": errs.get(x["key"], [])} for x in repo.list_rules(req.ctx, sid)]


@r("POST", "/api/systems/{sid:int}/rules")
def rules_create(req: Request) -> Any:
    return editing.create_rule(req.ctx, req.params["sid"], req.json())


@r("POST", "/api/systems/{sid:int}/rules/validate")
def rules_validate(req: Request) -> Any:
    data = req.json()
    exclude = data.get("id")
    try:
        errors = editing.check_rule(req.ctx, req.params["sid"], data, exclude_id=int(exclude) if exclude else None)
    except Invalid as e:
        errors = [e.message]
    return {"ok": not errors, "errors": errors}


@r("GET", "/api/rules/{id:int}")
def rules_get(req: Request) -> Any:
    """ルールの詳細（変更履歴つき）"""
    x = repo.get_rule(req.ctx, req.params["id"])
    x["revisions"] = versions.timeline(req.ctx, x["system_id"], "rule", x["key"])[:50]
    return x


@r("PUT", "/api/rules/{id:int}")
def rules_update(req: Request) -> Any:
    return editing.update_rule(req.ctx, req.params["id"], req.json())


@r("DELETE", "/api/rules/{id:int}")
def rules_delete(req: Request) -> Any:
    editing.delete_rule(req.ctx, req.params["id"])
    return {"ok": True}


# ---------------------------------------------------------------- グラフ・計算

@r("GET", "/api/systems/{sid:int}/graph")
def graph(req: Request) -> Any:
    sid = req.params["sid"]
    g = analysis.graph(req.ctx, sid)
    g["baseline_changes"] = baseline.changed_since_current(req.ctx, sid)
    return g


@r("GET", "/api/systems/{sid:int}/violations")
def violations(req: Request) -> Any:
    return [v.to_dict() for v in analysis.violations(req.ctx, req.params["sid"])]


@r("POST", "/api/systems/{sid:int}/recalc")
def recalc(req: Request) -> Any:
    data = req.json()
    return analysis.recalc(req.ctx, req.params["sid"], data.get("mode") or "stale", _str_list(data.get("refs"), "refs"))


@r("POST", "/api/systems/{sid:int}/apply-proposals")
def apply_proposals(req: Request) -> Any:
    data = req.json()
    refs = data.get("refs")
    return analysis.apply_proposals(req.ctx, req.params["sid"], None if refs is None else _str_list(refs, "refs"), data.get("comment") or "")


@r("GET", "/api/systems/{sid:int}/approvals")
def approvals(req: Request) -> Any:
    return param_state.approval_queue(req.ctx, req.params["sid"])


# ---------------------------------------------------------------- 版

@r("GET", "/api/systems/{sid:int}/baselines")
def baselines_list(req: Request) -> Any:
    sid = req.params["sid"]
    return {"baselines": baseline.list_baselines(req.ctx, sid), "readiness": baseline.readiness(req.ctx, sid)}


@r("POST", "/api/systems/{sid:int}/baselines")
def baselines_create(req: Request) -> Any:
    return baseline.create_baseline(req.ctx, req.params["sid"], req.json().get("label") or "")


@r("GET", "/api/baselines/{id:int}")
def baselines_get(req: Request) -> Any:
    return baseline.get_baseline(req.ctx, req.params["id"])


@r("POST", "/api/baselines/{id:int}/approve")
def baselines_approve(req: Request) -> Any:
    _require(req, "approver")
    return baseline.approve_baseline(req.ctx, req.params["id"], req.json().get("comment") or "")


@r("POST", "/api/baselines/{id:int}/reject")
def baselines_reject(req: Request) -> Any:
    _require(req, "approver")
    return baseline.reject_baseline(req.ctx, req.params["id"], req.json().get("comment") or "")


@r("POST", "/api/baselines/{id:int}/restore")
def baselines_restore(req: Request) -> Any:
    baseline.restore_baseline(req.ctx, req.params["id"], req.json().get("comment") or "")
    return {"ok": True}


@r("GET", "/api/systems/{sid:int}/diff")
def diff(req: Request) -> Any:
    return baseline.diff(req.ctx, req.params["sid"], req.query.get("from"), req.query.get("to"))


# ---------------------------------------------------------------- 履歴

@r("GET", "/api/systems/{sid:int}/revisions")
def revisions(req: Request) -> Any:
    q = req.query
    return history.list_revisions(req.ctx, req.params["sid"], q.get("entity"), q.get("key"), int(q.get("limit", 200)))


@r("GET", "/api/systems/{sid:int}/audit")
def audit(req: Request) -> Any:
    return history.list_audit(req.ctx, req.params["sid"], req.query.get("key"), int(req.query.get("limit", 200)))


@r("GET", "/api/audit")
def audit_all(req: Request) -> Any:
    """全体の監査ログ（システムの削除・テンプレート・設定の操作を含む）"""
    return history.list_audit(req.ctx, None, req.query.get("key"), int(req.query.get("limit", 200)))


@r("GET", "/api/revisions/{id:int}")
def revision_get(req: Request) -> Any:
    """リビジョン 1 件（変更前後の内容と差分）"""
    return history.get_revision(req.ctx, req.params["id"])


@r("POST", "/api/revisions/{id:int}/restore")
def revision_restore(req: Request) -> Any:
    """パラメータ・構成項目・ルールを、指定リビジョン直後の内容に戻す"""
    return versions.restore_revision(req.ctx, req.params["id"], req.json().get("comment") or "")


@r("GET", "/api/systems/{sid:int}/history/timeline")
def history_timeline(req: Request) -> Any:
    """1 エンティティの版の系列（entity=param|item|rule, key=参照名またはルールキー）"""
    return versions.timeline(req.ctx, req.params["sid"], req.query.get("entity") or "", req.query.get("key") or "")


@r("GET", "/api/systems/{sid:int}/history/state")
def history_state(req: Request) -> Any:
    """指定時点の内容を再現する（at=ISO 8601 の日時 または rev:<リビジョンID>）"""
    return versions.state_at(req.ctx, req.params["sid"], req.query.get("at"))


@r("GET", "/api/systems/{sid:int}/history/diff")
def history_diff(req: Request) -> Any:
    """2 時点間の差分（from / to = ISO 8601 の日時 または rev:<ID>。to 省略時は現在）"""
    return versions.diff(req.ctx, req.params["sid"], req.query.get("from"), req.query.get("to"))


# ---------------------------------------------------------------- AI

@r("POST", "/api/systems/{sid:int}/ai/{purpose}")
def ai_start(req: Request) -> Any:
    sid = req.params["sid"]
    cfg = jobs.effective_llm_config(req.ctx, req.app.config.llm)
    if not cfg.enabled:
        raise Invalid("LLM の接続先が設定されていません。設定画面で接続先とモデルを設定してください")
    targets = _str_list(req.json().get("targets"), "targets")
    run_id = jobs.create_run(req.ctx, sid, req.params["purpose"], targets, cfg.model)
    req.app.start_ai_run(req.ctx.actor, run_id, cfg)
    return {"run_id": run_id}


@r("GET", "/api/systems/{sid:int}/ai/runs")
def ai_runs(req: Request) -> Any:
    return jobs.list_runs(req.ctx, req.params["sid"])


@r("GET", "/api/ai/runs/{id:int}")
def ai_run(req: Request) -> Any:
    return jobs.get_run(req.ctx, req.params["id"])


@r("POST", "/api/ai/suggestions/{id:int}/adopt")
def ai_adopt(req: Request) -> Any:
    return jobs.decide_suggestion(req.ctx, req.params["id"], True)


@r("POST", "/api/ai/suggestions/{id:int}/reject")
def ai_reject(req: Request) -> Any:
    return jobs.decide_suggestion(req.ctx, req.params["id"], False)


@r("GET", "/api/settings/llm")
def llm_get(req: Request) -> Any:
    return jobs.get_settings(req.ctx, req.app.config.llm)


@r("PUT", "/api/settings/llm")
def llm_put(req: Request) -> Any:
    _require(req)
    return jobs.put_settings(req.ctx, req.app.config.llm, req.json())


# ---------------------------------------------------------------- テンプレート

@r("GET", "/api/templates")
def templates_list(req: Request) -> Any:
    return templates.list_templates(req.ctx)


@r("POST", "/api/templates")
def templates_create(req: Request) -> Any:
    data = req.json()
    if data.get("system_id"):
        return templates.template_from_system(req.ctx, int(data["system_id"]), data.get("name") or "", data.get("description") or "")
    if isinstance(data.get("payload"), dict):
        return templates.import_template(req.ctx, data["payload"], data.get("name"), data.get("description"))
    raise BadRequest("system_id または payload を指定してください")


@r("GET", "/api/templates/{id:int}")
def templates_get(req: Request) -> Any:
    return templates.get_template(req.ctx, req.params["id"])


@r("DELETE", "/api/templates/{id:int}")
def templates_delete(req: Request) -> Any:
    templates.delete_template(req.ctx, req.params["id"])
    return {"ok": True}


@r("GET", "/api/templates/{id:int}/export")
def templates_export(req: Request) -> Any:
    t = templates.get_template(req.ctx, req.params["id"])
    return _download(_json_bytes(t["payload"]), "application/json; charset=utf-8", f"template-{t['name']}-v{t['version']}.json")


# ---------------------------------------------------------------- 入出力

@r("GET", "/api/systems/{sid:int}/export")
def export_system(req: Request) -> Any:
    sid = req.params["sid"]
    fmt = req.query.get("format", "json")
    name = repo.get_system(req.ctx, sid)["name"]
    if fmt == "json":
        return _download(_json_bytes(snapshot.export_payload(req.ctx, sid)), "application/json; charset=utf-8", f"{name}.json")
    if fmt == "rules-json":
        return _download(_json_bytes(snapshot.export_rules(req.ctx, sid)), "application/json; charset=utf-8", f"{name}-rules.json")
    if fmt == "csv":
        return _download(export.to_csv(req.ctx, sid), "text/csv; charset=utf-8", f"{name}-params.csv")
    if fmt == "html":
        return _download(export.to_html(req.ctx, sid), "text/html; charset=utf-8", f"{name}.html")
    raise BadRequest(f"未対応の形式です: {fmt}")


@r("POST", "/api/systems/{sid:int}/import")
def import_system(req: Request) -> Any:
    sid = req.params["sid"]
    data = req.json()
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise BadRequest("payload（JSON オブジェクト）を指定してください")
    if payload.get("format") == snapshot.RULES_FORMAT:
        return snapshot.import_rules(req.ctx, sid, payload)
    snapshot.replace_with_payload(req.ctx, sid, payload, keep_status=bool(data.get("keep_status")), op="update", comment="JSON 取り込み")
    templates._mark_formula_targets_stale(req.ctx, sid)
    history.audit(req.ctx, sid, "system.import", "system", None)
    return {"ok": True}


# API 一覧（GET /api）に出す説明。docstring を持たないハンドラの分をここで補う
_SUMMARIES = {
    "meta": "操作者・ラベル類・LLM 設定などの画面用メタ情報",
    "systems_list": "システムの一覧", "systems_create": "システムを作成する（template_id 指定でテンプレートから）",
    "systems_get": "システム 1 件", "systems_update": "システムの名前・説明を更新する", "systems_delete": "システムを削除する",
    "systems_clone": "システムを複製する",
    "layers_list": "レイヤーの一覧", "layers_create": "レイヤーを追加する", "layers_reorder": "レイヤーを並べ替える（layer_ids）",
    "layers_get": "レイヤー 1 件", "layers_update": "レイヤーのキー・名前を更新する", "layers_delete": "空のレイヤーを削除する",
    "params_list": "パラメータの一覧", "params_create": "パラメータを作成する", "params_get": "パラメータの詳細（履歴・監査ログつき）",
    "params_update": "パラメータを更新する（値を変えると下書きに戻り、下流に要再計算が付く）", "params_delete": "パラメータを削除する",
    "params_transition": "パラメータの状態遷移（skip_ai / request_review / approve / reject / withdraw / ack_upstream）",
    "items_delete": "構成項目を削除する",
    "relations_list": "手動の関連の一覧", "relations_create": "関連を登録する（from_ref, to_ref, note）",
    "relations_get": "関連 1 件", "relations_delete": "関連を削除する",
    "rules_list": "ルールの一覧（検証エラーつき）", "rules_create": "ルールを作成する", "rules_validate": "ルール案を保存せずに検証する",
    "rules_update": "ルールを更新する", "rules_delete": "ルールを削除する",
    "graph": "関連図のデータ（ノード・エッジ・違反・座標）", "violations": "制約違反の一覧",
    "recalc": "再計算案を作成する（mode: stale / downstream / all）", "apply_proposals": "再計算案を値に反映する",
    "approvals": "承認待ちの一覧（パラメータ・構成項目・版）",
    "baselines_list": "版の一覧と、作成できるかの判定", "baselines_create": "版を作成してレビューに出す", "baselines_get": "版 1 件（凍結内容つき）",
    "baselines_approve": "版を承認する", "baselines_reject": "版を差し戻す", "baselines_restore": "作業中の状態を版の内容で置き換える",
    "diff": "版同士、または版と作業中の差分（from / to = working / current / 版 ID）",
    "revisions": "変更履歴（entity, key, limit で絞り込み）", "audit": "システムの監査ログ",
    "ai_start": "AI ジョブを開始する（evaluate / suggest_values / suggest_rules / rationale）", "ai_runs": "AI ジョブの一覧",
    "ai_run": "AI ジョブ 1 件（提案つき）", "ai_adopt": "AI の提案を採用する", "ai_reject": "AI の提案を不採用にする",
    "llm_get": "LLM の接続設定", "llm_put": "LLM の接続設定を更新する",
    "templates_list": "テンプレートの一覧", "templates_create": "テンプレートを作成する（system_id または payload）",
    "templates_get": "テンプレート 1 件", "templates_delete": "テンプレートを削除する", "templates_export": "テンプレートを JSON で書き出す",
    "export_system": "システムを書き出す（format = json / rules-json / csv / html）",
    "import_system": "JSON を取り込む（reqviz/system は置き換え、reqviz/rules は追加・更新）",
}
for _name, _doc in _SUMMARIES.items():
    if not globals()[_name].__doc__:
        globals()[_name].__doc__ = _doc


def content_disposition(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"
