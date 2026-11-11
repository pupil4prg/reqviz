"""A2A（Agent2Agent）プロトコル v1.0 の JSON-RPC バインディング。

- ``GET /.well-known/agent-card.json``: Agent Card
- ``POST /a2a``: ``SendMessage`` / ``GetTask`` / ``ListTasks`` / ``CancelTask``

ストリーミングとプッシュ通知は提供しない。タスクは DB（a2a_tasks）に保存し、
``contextId`` が同じタスクの間で会話を引き継ぐ。
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .. import db
from ..agent.dialog import COMPLETED, FAILED, MAX_ARTIFACT_CHARS, DialogEngine, Turn
from ..agent.tools import ApiClient
from ..config import LLMConfig
from ..context import Ctx

PROTOCOL_VERSION = "1.0"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
STATE_COMPLETED = "TASK_STATE_COMPLETED"
STATE_FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"
STATE_INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
REJECTED = "TASK_STATE_REJECTED"
TERMINAL = (STATE_COMPLETED, STATE_FAILED, CANCELED, REJECTED)
ALL_STATES = (SUBMITTED, WORKING, STATE_COMPLETED, STATE_FAILED, CANCELED, STATE_INPUT_REQUIRED, REJECTED, "TASK_STATE_AUTH_REQUIRED")
_OUTCOME_STATE = {COMPLETED: STATE_COMPLETED, FAILED: STATE_FAILED, "input_required": STATE_INPUT_REQUIRED}

# (JSON-RPC コード, ErrorInfo.reason)
E_PARSE = (-32700, "JSON_PARSE_ERROR")
E_INVALID_REQUEST = (-32600, "INVALID_REQUEST")
E_METHOD_NOT_FOUND = (-32601, "METHOD_NOT_FOUND")
E_INVALID_PARAMS = (-32602, "INVALID_PARAMS")
E_INTERNAL = (-32603, "INTERNAL_ERROR")
E_TASK_NOT_FOUND = (-32001, "TASK_NOT_FOUND")
E_NOT_CANCELABLE = (-32002, "TASK_NOT_CANCELABLE")
E_PUSH_UNSUPPORTED = (-32003, "PUSH_NOTIFICATION_NOT_SUPPORTED")
E_UNSUPPORTED_OPERATION = (-32004, "UNSUPPORTED_OPERATION")
E_CONTENT_TYPE = (-32005, "CONTENT_TYPE_NOT_SUPPORTED")
E_NO_EXTENDED_CARD = (-32007, "EXTENDED_AGENT_CARD_NOT_CONFIGURED")
E_VERSION = (-32009, "VERSION_NOT_SUPPORTED")


class A2AError(Exception):
    def __init__(self, kind: tuple[int, str], message: str, metadata: dict[str, str] | None = None):
        super().__init__(message)
        self.code, self.reason = kind
        self.message = message
        self.metadata = metadata or {}


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def agent_card(base_url: str, version: str) -> dict[str, Any]:
    modes = ["text/plain", "application/json"]
    return {
        "name": "reqviz",
        "description": (
            "システムの要件から設計までの構成情報（パラメータ、実装方式などの選択肢、不定形データ、算出式・制約ルール）と、"
            "その承認・版・変更履歴を管理するエージェント。文章での依頼、またはツールを直接指定した構造化データを受け付ける。"
        ),
        "supportedInterfaces": [
            {"url": base_url.rstrip("/") + "/a2a", "protocolBinding": "JSONRPC", "protocolVersion": PROTOCOL_VERSION}
        ],
        "version": version,
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": modes,
        "defaultOutputModes": modes,
        "skills": [
            {
                "id": "config-query",
                "name": "構成情報の参照",
                "description": "パラメータ・選択肢・不定形データ・ルール・制約違反・承認待ちを調べて答える。",
                "tags": ["configuration", "query"],
                "examples": ["販売管理システムの DB コネクション数とその根拠は？", "制約違反が出ている項目を教えて"],
            },
            {
                "id": "config-change",
                "name": "構成情報の変更",
                "description": "値・採用する実装方式・本文・ルールを変更し、再計算と承認依頼まで行う。変更の前に内容を示して確認を求める"
                               "（リクエストの metadata.autoApprove = true で省略できる）。",
                "tags": ["configuration", "change", "approval"],
                "examples": ["セッション管理方式を redis に変更して", "ピーク TPS を 300 にして下流を再計算して"],
            },
            {
                "id": "config-history",
                "name": "履歴と版",
                "description": "変更履歴、過去の時点の内容、時点間や版の間の差分を調べ、過去の内容に戻す。",
                "tags": ["history", "versioning", "audit"],
                "examples": ["先週から何が変わった？", "app.server_count を 1 つ前の内容に戻して"],
            },
            {
                "id": "tool-invoke",
                "name": "ツールの直接実行",
                "description": "LLM を介さずに操作を実行する。データパートに {\"tool\": 名前, \"arguments\": {...}} を入れるか、"
                               "テキストで「ツール名 {JSON 引数}」と送る。「help」でツール一覧を返す。",
                "tags": ["tools", "structured"],
                "examples": ["help", "list_params {\"system_id\": 1}"],
                "inputModes": modes,
                "outputModes": ["application/json"],
            },
        ],
    }


# ---------------------------------------------------------------- タスクの保存

_JSON_COLS = ("status_message", "history", "artifacts", "agent_state", "metadata")


def _row_to_task(r: Any) -> dict[str, Any]:
    d = dict(r)
    for c in _JSON_COLS:
        d[c] = db.loads(d[c])
    return d


def load_task(ctx: Ctx, task_id: Any) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM a2a_tasks WHERE id = ?", (str(task_id),)).fetchone() if isinstance(task_id, str) else None
    if r is None:
        raise A2AError(E_TASK_NOT_FOUND, "Task not found", {"taskId": str(task_id)})
    return _row_to_task(r)


def save_task(ctx: Ctx, t: dict[str, Any], insert: bool = False) -> None:
    t["updated_at"] = db.now()
    vals = {c: (None if t.get(c) is None else db.dumps(t[c])) for c in _JSON_COLS}
    if insert:
        ctx.conn.execute(
            "INSERT INTO a2a_tasks (id, context_id, state, status_message, status_at, history, artifacts, agent_state,"
            " metadata, actor, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t["id"], t["context_id"], t["state"], vals["status_message"], t["status_at"], vals["history"], vals["artifacts"],
             vals["agent_state"], vals["metadata"], t["actor"], t["created_at"], t["updated_at"]),
        )
    else:
        ctx.conn.execute(
            "UPDATE a2a_tasks SET state = ?, status_message = ?, status_at = ?, history = ?, artifacts = ?, agent_state = ?,"
            " updated_at = ? WHERE id = ?",
            (t["state"], vals["status_message"], t["status_at"], vals["history"], vals["artifacts"], vals["agent_state"],
             t["updated_at"], t["id"]),
        )


def task_json(t: dict[str, Any], history_length: Any = None, include_artifacts: bool = True) -> dict[str, Any]:
    status: dict[str, Any] = {"state": t["state"], "timestamp": t["status_at"]}
    if t.get("status_message"):
        status["message"] = t["status_message"]
    out: dict[str, Any] = {"id": t["id"], "contextId": t["context_id"], "status": status}
    if include_artifacts and t["artifacts"]:
        out["artifacts"] = t["artifacts"]
    hist = t["history"]
    if isinstance(history_length, int) and not isinstance(history_length, bool) and history_length >= 0:
        hist = hist[len(hist) - history_length:] if history_length else []
    if hist:
        out["history"] = hist
    if t["metadata"]:
        out["metadata"] = t["metadata"]
    return out


def _set_status(t: dict[str, Any], state: str, text: str | None = None) -> None:
    t["state"] = state
    t["status_at"] = _timestamp()
    t["status_message"] = None
    if text is not None:
        msg = {
            "messageId": str(uuid.uuid4()), "role": "ROLE_AGENT", "parts": [{"text": text}],
            "taskId": t["id"], "contextId": t["context_id"],
        }
        t["status_message"] = msg
        t["history"].append(msg)


# ---------------------------------------------------------------- サーバー

LlmProvider = Callable[[Ctx], LLMConfig]


class A2AServer:
    def __init__(
        self,
        db_path: Any,
        client: ApiClient,
        llm_provider: LlmProvider,
        version: str = "0.0.0",
        opener: Callable[..., Any] | None = None,
        run_in_thread: bool = True,
    ):
        self.db_path = db_path
        self.client = client
        self.llm_provider = llm_provider
        self.version = version
        self.opener = opener
        self.run_in_thread = run_in_thread

    # ------------------------------------------------------------ JSON-RPC

    def handle(self, body: bytes, headers: Mapping[str, str], query: Mapping[str, str], actor: str) -> dict[str, Any]:
        msg_id: Any = None
        try:
            try:
                msg = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise A2AError(E_PARSE, "Invalid JSON payload") from None
            if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
                raise A2AError(E_INVALID_REQUEST, "Request payload validation error")
            msg_id = msg.get("id")
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                raise A2AError(E_INVALID_PARAMS, "Invalid parameters: params must be an object")

            version = (headers.get("A2A-Version") or query.get("A2A-Version") or "").strip()
            if ".".join(version.split(".")[:2]) != PROTOCOL_VERSION:
                raise A2AError(
                    E_VERSION,
                    f"A2A protocol version {version or '0.3 (A2A-Version header is empty)'} is not supported; "
                    f"send 'A2A-Version: {PROTOCOL_VERSION}'",
                    {"supportedVersions": PROTOCOL_VERSION},
                )
            result = self._dispatch(msg["method"], params, actor)
            return {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except A2AError as e:
            return self._error(msg_id, e)
        except Exception as e:  # pragma: no cover - 想定外
            return self._error(msg_id, A2AError(E_INTERNAL, f"Internal error: {type(e).__name__}: {e}"))

    @staticmethod
    def _error(msg_id: Any, e: A2AError) -> dict[str, Any]:
        info = {
            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
            "reason": e.reason, "domain": "a2a-protocol.org", "metadata": e.metadata,
        }
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": e.code, "message": e.message, "data": [info]}}

    def _dispatch(self, method: str, params: dict[str, Any], actor: str) -> dict[str, Any]:
        if method in ("SendStreamingMessage", "SubscribeToTask"):
            raise A2AError(E_UNSUPPORTED_OPERATION, "Streaming is not supported by this agent")
        if method.endswith("TaskPushNotificationConfig") or method.endswith("TaskPushNotificationConfigs"):
            raise A2AError(E_PUSH_UNSUPPORTED, "Push notifications are not supported by this agent")
        if method == "GetExtendedAgentCard":
            raise A2AError(E_NO_EXTENDED_CARD, "This agent has no extended agent card")
        handler = {
            "SendMessage": self._send_message, "GetTask": self._get_task,
            "ListTasks": self._list_tasks, "CancelTask": self._cancel_task,
        }.get(method)
        if handler is None:
            raise A2AError(E_METHOD_NOT_FOUND, f"Method not found: {method}")
        conn = db.connect(self.db_path)
        try:
            return handler(Ctx(conn, actor), params)
        finally:
            conn.close()

    # ------------------------------------------------------------ メソッド

    def _send_message(self, ctx: Ctx, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message")
        if not isinstance(message, dict):
            raise A2AError(E_INVALID_PARAMS, "Invalid parameters: message is required")
        if not isinstance(message.get("messageId"), str) or not message["messageId"]:
            raise A2AError(E_INVALID_PARAMS, "Invalid parameters: message.messageId is required")
        if message.get("role") != "ROLE_USER":
            raise A2AError(E_INVALID_PARAMS, "Invalid parameters: message.role must be ROLE_USER")
        parts = message.get("parts")
        if not isinstance(parts, list) or not parts or not all(isinstance(p, dict) for p in parts):
            raise A2AError(E_INVALID_PARAMS, "Invalid parameters: message.parts requires at least one part")
        texts, data_parts = [], []
        for p in parts:
            if isinstance(p.get("text"), str):
                texts.append(p["text"])
            elif "data" in p:
                data_parts.append(p["data"])
            else:
                raise A2AError(E_CONTENT_TYPE, "Only text and data parts are supported (no files)")

        config = params.get("configuration") if isinstance(params.get("configuration"), dict) else {}
        meta: dict[str, Any] = {}
        for m in (params.get("metadata"), message.get("metadata")):
            if isinstance(m, dict):
                meta.update(m)
        auto_approve = meta.get("autoApprove") is True

        task_id, context_id = message.get("taskId"), message.get("contextId")
        if task_id:
            t = load_task(ctx, task_id)
            if context_id and context_id != t["context_id"]:
                raise A2AError(E_INVALID_PARAMS, "Invalid parameters: contextId does not match the task")
            if t["state"] in TERMINAL:
                raise A2AError(E_UNSUPPORTED_OPERATION, f"Task is in a terminal state ({t['state']}) and cannot accept messages")
            if t["state"] in (SUBMITTED, WORKING):
                raise A2AError(E_UNSUPPORTED_OPERATION, "Task is still being processed")
            insert = False
        else:
            if context_id is not None and (not isinstance(context_id, str) or not context_id or len(context_id) > 128):
                raise A2AError(E_INVALID_PARAMS, "Invalid parameters: contextId must be a non-empty string (max 128 chars)")
            state: dict[str, Any] = {}
            if context_id:
                prev = ctx.conn.execute(
                    "SELECT agent_state FROM a2a_tasks WHERE context_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1", (context_id,)
                ).fetchone()
                if prev:  # 同じ文脈の直前のタスクから会話を引き継ぐ
                    state = {"messages": (db.loads(prev["agent_state"]) or {}).get("messages") or []}
            ts = db.now()
            t = {
                "id": str(uuid.uuid4()), "context_id": context_id or str(uuid.uuid4()), "state": SUBMITTED,
                "status_message": None, "status_at": _timestamp(), "history": [], "artifacts": [], "agent_state": state,
                "metadata": {}, "actor": ctx.actor, "created_at": ts,
            }
            insert = True

        stored = {k: v for k, v in message.items() if k in ("messageId", "role", "parts", "metadata", "extensions", "referenceTaskIds")}
        stored.update(taskId=t["id"], contextId=t["context_id"])
        t["history"].append(stored)
        _set_status(t, WORKING)
        save_task(ctx, t, insert=insert)

        work = (t["id"], "\n".join(texts), data_parts, auto_approve, ctx.actor)
        if config.get("returnImmediately") is True and self.run_in_thread:
            threading.Thread(target=self._process_detached, args=work, daemon=True).start()
        else:
            self._process(ctx, *work)
            t = load_task(ctx, t["id"])
        return {"task": task_json(t, config.get("historyLength"))}

    def _process_detached(self, *work: Any) -> None:
        conn = db.connect(self.db_path)
        try:
            self._process(Ctx(conn, work[-1]), *work)
        finally:
            conn.close()

    def _process(self, ctx: Ctx, task_id: str, text: str, data_parts: list[Any], auto_approve: bool, actor: str) -> None:
        """対話エンジンを 1 回動かし、結果をタスクに反映する。"""
        t = load_task(ctx, task_id)
        try:
            engine = DialogEngine(self.client, self.llm_provider(ctx), actor, self.opener)
            turn = engine.run(text, data_parts, t["agent_state"], auto_approve)
        except Exception as e:  # 対話エンジンの失敗はタスクの失敗として記録する
            turn = Turn(FAILED, f"内部エラー: {type(e).__name__}: {e}", [], t["agent_state"])
        current = load_task(ctx, task_id)
        if current["state"] == CANCELED:
            return  # 処理中にキャンセルされた
        t = current
        t["agent_state"] = turn.state
        state = _OUTCOME_STATE[turn.outcome]
        if state == STATE_COMPLETED:
            t["artifacts"].append({"artifactId": str(uuid.uuid4()), "name": "response", "parts": [{"text": turn.text}]})
        for r in turn.results:
            if not r.get("ok"):
                continue
            part = {"data": {"tool": r["tool"], "arguments": r["arguments"], "result": r["result"]}, "mediaType": "application/json"}
            if len(json.dumps(part, ensure_ascii=False)) <= MAX_ARTIFACT_CHARS:
                t["artifacts"].append({"artifactId": str(uuid.uuid4()), "name": r["tool"], "parts": [part]})
        _set_status(t, state, turn.text)
        save_task(ctx, t)

    def _get_task(self, ctx: Ctx, params: dict[str, Any]) -> dict[str, Any]:
        return task_json(load_task(ctx, params.get("id")), params.get("historyLength"))

    def _list_tasks(self, ctx: Ctx, params: dict[str, Any]) -> dict[str, Any]:
        where, args = ["1 = 1"], []
        if params.get("contextId"):
            where.append("context_id = ?")
            args.append(str(params["contextId"]))
        if params.get("status"):
            if params["status"] not in ALL_STATES:
                raise A2AError(E_INVALID_PARAMS, f"Invalid parameters: unknown status {params['status']!r}")
            where.append("state = ?")
            args.append(params["status"])
        size = params.get("pageSize", 50)
        if not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= 100:
            raise A2AError(E_INVALID_PARAMS, "Invalid parameters: pageSize must be between 1 and 100")
        try:
            offset = int(params.get("pageToken") or 0)
        except (TypeError, ValueError):
            raise A2AError(E_INVALID_PARAMS, "Invalid parameters: pageToken is invalid") from None
        cond = " AND ".join(where)
        total = ctx.conn.execute(f"SELECT COUNT(*) FROM a2a_tasks WHERE {cond}", args).fetchone()[0]
        rows = ctx.conn.execute(
            f"SELECT * FROM a2a_tasks WHERE {cond} ORDER BY updated_at DESC, rowid DESC LIMIT ? OFFSET ?", [*args, size, max(offset, 0)]
        ).fetchall()
        history_length = params.get("historyLength", 0)
        tasks = [task_json(_row_to_task(r), history_length, params.get("includeArtifacts") is True) for r in rows]
        nxt = offset + len(rows)
        return {"tasks": tasks, "nextPageToken": str(nxt) if nxt < total else "", "pageSize": size, "totalSize": total}

    def _cancel_task(self, ctx: Ctx, params: dict[str, Any]) -> dict[str, Any]:
        t = load_task(ctx, params.get("id"))
        if t["state"] in TERMINAL:
            raise A2AError(E_NOT_CANCELABLE, f"Task is already in a terminal state ({t['state']})", {"taskId": t["id"]})
        t["agent_state"].pop("pending", None)
        _set_status(t, CANCELED, "キャンセルしました。")
        save_task(ctx, t)
        return task_json(t)
