"""REST API のルーティング定義。ハンドラは薄く保ち、処理は workflow / ai / io に委ねる。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from ..ai import jobs
from ..ai.prompts import PURPOSE_LABELS
from ..errors import BadRequest, Invalid
from ..io import export
from ..repo import history
from ..repo import model as repo
from ..router import Request, Response, Router
from ..workflow import analysis, baseline, editing, param_state, snapshot, templates

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


# ---------------------------------------------------------------- メタ

@r("GET", "/api/meta")
def meta(req: Request) -> Any:
    return {
        "user": req.ctx.actor,
        "status_labels": param_state.STATUS_LABELS,
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


@r("GET", "/api/params/{id:int}")
def params_get(req: Request) -> Any:
    p = repo.get_param(req.ctx, req.params["id"])
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
    return param_state.transition(req.ctx, req.params["id"], data.get("action") or "", data.get("comment") or "", data.get("row_version"))


@r("POST", "/api/systems/{sid:int}/transition")
def params_bulk_transition(req: Request) -> Any:
    """複数パラメータへの一括操作。失敗したものは理由を返す。"""
    data = req.json()
    ok, failed = [], []
    for pid in _int_list(data.get("param_ids"), "param_ids"):
        try:
            ok.append(param_state.transition(req.ctx, pid, data.get("action") or "", data.get("comment") or ""))
        except Invalid as e:
            failed.append({"id": pid, "message": e.message, "details": e.details})
    return {"ok": ok, "failed": failed}


# ---------------------------------------------------------------- 関連

@r("GET", "/api/systems/{sid:int}/relations")
def relations_list(req: Request) -> Any:
    return repo.list_relations(req.ctx, req.params["sid"])


@r("POST", "/api/systems/{sid:int}/relations")
def relations_create(req: Request) -> Any:
    return editing.create_relation(req.ctx, req.params["sid"], req.json())


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
    return baseline.approve_baseline(req.ctx, req.params["id"], req.json().get("comment") or "")


@r("POST", "/api/baselines/{id:int}/reject")
def baselines_reject(req: Request) -> Any:
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


def content_disposition(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename)}"
