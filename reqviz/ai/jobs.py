"""AI ジョブの実行・記録と、提案の採用／不採用。"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from .. import db
from ..config import LLMConfig
from ..context import Ctx
from ..db import dumps, loads, now, transaction
from ..errors import Invalid, NotFound
from ..repo import history
from ..repo import model as repo
from ..workflow import analysis, editing, param_state
from . import client, prompts, schema

SETTING_KEYS = {"llm.base_url": "base_url", "llm.model": "model", "llm.timeout": "timeout"}


# ---------------------------------------------------------------- 設定

def effective_llm_config(ctx: Ctx, base: LLMConfig) -> LLMConfig:
    """環境変数の設定に、画面で保存した設定（DB）を上書きした接続設定。API キーは環境変数のみ。"""
    cfg = replace(base)
    for row in ctx.conn.execute("SELECT key, value FROM settings WHERE key LIKE 'llm.%'"):
        attr = SETTING_KEYS.get(row["key"])
        if attr == "timeout":
            cfg.timeout = float(row["value"])
        elif attr and row["value"]:
            setattr(cfg, attr, row["value"].rstrip("/") if attr == "base_url" else row["value"])
    return cfg


def get_settings(ctx: Ctx, base: LLMConfig) -> dict[str, Any]:
    cfg = effective_llm_config(ctx, base)
    return {
        "base_url": cfg.base_url,
        "model": cfg.model,
        "timeout": cfg.timeout,
        "api_key_set": bool(cfg.api_key),
        "enabled": cfg.enabled,
    }


def put_settings(ctx: Ctx, base: LLMConfig, data: dict[str, Any]) -> dict[str, Any]:
    with transaction(ctx.conn):
        for key, attr in SETTING_KEYS.items():
            if attr not in data:
                continue
            value = data[attr]
            if attr == "timeout":
                try:
                    value = str(max(1.0, min(600.0, float(value))))
                except (TypeError, ValueError):
                    raise Invalid("タイムアウトは秒数で指定してください") from None
            ctx.conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value or "")),
            )
        history.audit(ctx, None, "settings.llm", "settings", None, {k: v for k, v in data.items() if k in SETTING_KEYS.values()})
    return get_settings(ctx, base)


# ---------------------------------------------------------------- コンテキスト

def build_context(ctx: Ctx, system_id: int) -> dict[str, Any]:
    g = analysis.graph(ctx, system_id)
    layer_names = {l["key"]: l["name"] for l in g["layers"]}
    return {
        "system": {"name": g["system"]["name"], "description": g["system"]["description"]},
        "layers": [{"key": l["key"], "name": l["name"]} for l in g["layers"]],
        "params": [
            {
                "ref": n["ref"], "name": n["name"], "layer": layer_names.get(n["layer_key"]),
                "type": n["type"], "options": n["enum_options"] or None, "value": n["value"],
                "unit": n["unit"], "rationale": n["rationale"] or None, "formula": n["formula"],
                "status": n["status"],
            }
            for n in g["nodes"]
        ],
        "rules": [
            {k: r[k] for k in ("key", "kind", "target", "expression", "nl_text", "severity", "description") if r.get(k)}
            for r in repo.list_rules(ctx, system_id)
            if r["enabled"]
        ],
        "violations": g["violations"],
    }


# ---------------------------------------------------------------- 実行

def create_run(ctx: Ctx, system_id: int, purpose: str, targets: list[str], model: str) -> int:
    if purpose not in prompts.PURPOSES:
        raise Invalid(f"不明な AI 処理です: {purpose}")
    by_ref = repo.params_by_ref(ctx, system_id)
    unknown = [t for t in targets if t not in by_ref]
    if unknown:
        raise Invalid(f"存在しないパラメータです: {', '.join(unknown)}")
    with transaction(ctx.conn):
        cur = ctx.conn.execute(
            "INSERT INTO ai_runs (system_id, purpose, targets, status, model, actor, created_at) VALUES (?, ?, ?, 'queued', ?, ?, ?)",
            (system_id, purpose, dumps(targets), model, ctx.actor, now()),
        )
    return cur.lastrowid


def execute_run(ctx: Ctx, run_id: int, cfg: LLMConfig, opener: Callable[..., Any] | None = None) -> dict[str, Any]:
    """ジョブを同期実行する（スレッドから呼ばれる。テストでは直接呼ぶ）。"""
    run = get_run(ctx, run_id)
    sid, purpose, targets = run["system_id"], run["purpose"], run["targets"]
    ctx.conn.execute("UPDATE ai_runs SET status = 'running' WHERE id = ?", (run_id,))
    messages = prompts.build_messages(purpose, build_context(ctx, sid), targets)
    ctx.conn.execute("UPDATE ai_runs SET request_json = ? WHERE id = ?", (dumps(messages), run_id))
    try:
        kwargs = {"opener": opener} if opener else {}
        content, _raw = client.chat(cfg, messages, **kwargs)
        ctx.conn.execute("UPDATE ai_runs SET response_json = ? WHERE id = ?", (content, run_id))
        data = client.parse_json(content)
        errs = schema.validate(data, prompts.SCHEMAS[purpose])
        if errs:
            raise client.LLMError("応答が想定の形式ではありません: " + "; ".join(errs[:5]))
        with transaction(ctx.conn):
            _store_suggestions(ctx, run_id, purpose, data, sid)
            if purpose == "evaluate":
                _mark_evaluated(ctx, sid, targets)
            ctx.conn.execute(
                "UPDATE ai_runs SET status = 'done', finished_at = ? WHERE id = ?", (now(), run_id)
            )
            history.audit(ctx, sid, f"ai.{purpose}", "ai_run", str(run_id), {"targets": targets})
    except Exception as e:  # LLM エラー以外（バグ）も記録して画面に出す
        ctx.conn.execute(
            "UPDATE ai_runs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            (str(e) if isinstance(e, client.LLMError) else f"{type(e).__name__}: {e}", now(), run_id),
        )
    return get_run(ctx, run_id)


def start_in_thread(db_path: Path, actor: str, run_id: int, cfg: LLMConfig) -> threading.Thread:
    def work() -> None:
        conn = db.connect(db_path)
        try:
            execute_run(Ctx(conn, actor), run_id, cfg)
        finally:
            conn.close()

    t = threading.Thread(target=work, name=f"ai-run-{run_id}", daemon=True)
    t.start()
    return t


def _store_suggestions(ctx: Ctx, run_id: int, purpose: str, data: dict[str, Any], system_id: int) -> None:
    known = set(repo.params_by_ref(ctx, system_id))

    def add(kind: str, target: str | None, payload: dict[str, Any], status: str = "pending") -> None:
        ctx.conn.execute(
            "INSERT INTO ai_suggestions (run_id, kind, target, payload_json, status) VALUES (?, ?, ?, ?, ?)",
            (run_id, kind, target, dumps(payload), status),
        )

    if purpose == "evaluate":
        if data.get("summary"):
            add("finding", None, {"severity": "info", "message": data["summary"], "summary": True}, "info")
        for f in data["findings"]:
            add("finding", f.get("param") if f.get("param") in known else None, f, "info")
    elif purpose == "suggest_values":
        for s in data["suggestions"]:
            if s["param"] in known:
                add("value", s["param"], s)
    elif purpose == "suggest_rules":
        for r in data["rules"]:
            add("rule", r.get("target"), r)
        for rel in data.get("relations", []):
            add("relation", rel.get("to"), rel)
    elif purpose == "rationale":
        for r in data["rationales"]:
            if r["param"] in known:
                add("rationale", r["param"], r)


def _mark_evaluated(ctx: Ctx, system_id: int, targets: list[str]) -> None:
    by_ref = repo.params_by_ref(ctx, system_id)
    refs = targets or list(by_ref)
    for ref in refs:
        p = by_ref.get(ref)
        if p and p["status"] == "draft":
            param_state.transition(ctx, p["id"], "ai_evaluate")


# ---------------------------------------------------------------- 参照

def _suggestion_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["payload"] = loads(d.pop("payload_json"))
    return d


def get_run(ctx: Ctx, run_id: int) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM ai_runs WHERE id = ?", (run_id,)).fetchone()
    if r is None:
        raise NotFound(f"AI 実行が見つかりません: {run_id}")
    d = dict(r)
    d["targets"] = loads(d["targets"], [])
    d["request"] = loads(d.pop("request_json"))
    d["response"] = d.pop("response_json")
    d["suggestions"] = [
        _suggestion_dict(s)
        for s in ctx.conn.execute("SELECT * FROM ai_suggestions WHERE run_id = ? ORDER BY id", (run_id,))
    ]
    return d


def list_runs(ctx: Ctx, system_id: int, limit: int = 30) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        "SELECT id, purpose, targets, status, model, error, actor, created_at, finished_at FROM ai_runs"
        " WHERE system_id = ? ORDER BY id DESC LIMIT ?",
        (system_id, limit),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["targets"] = loads(d["targets"], [])
        out.append(d)
    return out


# ---------------------------------------------------------------- 採用 / 不採用

def decide_suggestion(ctx: Ctx, suggestion_id: int, adopt: bool) -> dict[str, Any]:
    row = ctx.conn.execute(
        "SELECT s.*, r.system_id FROM ai_suggestions s JOIN ai_runs r ON r.id = s.run_id WHERE s.id = ?",
        (suggestion_id,),
    ).fetchone()
    if row is None:
        raise NotFound(f"提案が見つかりません: {suggestion_id}")
    s = _suggestion_dict(row)
    if s["status"] != "pending":
        raise Invalid("この提案は既に処理済みです")
    sid = s["system_id"]
    payload = s["payload"]
    with transaction(ctx.conn):
        if adopt:
            by_ref = repo.params_by_ref(ctx, sid)
            if s["kind"] == "value":
                p = by_ref.get(s["target"])
                if p is None:
                    raise Invalid(f"パラメータが見つかりません: {s['target']}")
                data: dict[str, Any] = {"value": payload["value"], "source": "ai"}
                if not p["rationale"]:
                    data["rationale"] = payload.get("rationale", "")
                editing.update_param(ctx, p["id"], data, comment="AI 提案を採用")
            elif s["kind"] == "rationale":
                p = by_ref.get(s["target"])
                if p is None:
                    raise Invalid(f"パラメータが見つかりません: {s['target']}")
                editing.update_param(ctx, p["id"], {"rationale": payload["text"]}, comment="AI 提案を採用")
            elif s["kind"] == "rule":
                editing.create_rule(ctx, sid, {k: payload.get(k) for k in editing.RULE_SNAPSHOT_FIELDS})
            elif s["kind"] == "relation":
                editing.create_relation(ctx, sid, {"from_ref": payload["from"], "to_ref": payload["to"], "note": payload.get("note", "")})
            else:
                raise Invalid("この提案は採用できる種類ではありません")
        ctx.conn.execute(
            "UPDATE ai_suggestions SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
            ("adopted" if adopt else "rejected", ctx.actor, now(), suggestion_id),
        )
        history.audit(ctx, sid, f"ai.suggestion.{'adopt' if adopt else 'reject'}", s["kind"], s["target"], payload)
    return _suggestion_dict(
        ctx.conn.execute("SELECT * FROM ai_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
    )
