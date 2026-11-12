"""MCP のメッセージ処理（トランスポート非依存）。

対応する版:
- 2026-07-28（ステートレス。リクエストごとに ``_meta`` で版と能力を宣言し、``server/discover`` を持つ）
- 2025-11-25 / 2025-06-18 / 2025-03-26（``initialize`` ハンドシェイクを使う旧方式）

セッションは持たない。旧方式のクライアントにも ``Mcp-Session-Id`` は発行しない。
公開する機能はツールのみ（resources / prompts は提供しない）。
"""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping

from ..agent.tools import TOOLS, TOOLS_BY_NAME, ApiClient, ToolError, call_tool

MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED_VERSIONS = MODERN_VERSIONS + LEGACY_VERSIONS

META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022

TOOLS_TTL_MS = 3_600_000  # ツール定義はプロセスの生存中は変わらない

INSTRUCTIONS = (
    "reqviz は、システムの要件から設計までの構成情報（パラメータ、実装方式などの選択肢、不定形データ、"
    "算出式・制約ルール）と、その承認・版・変更履歴を管理する。"
    "まず list_systems でシステム ID を確認する。本番・開発などの環境は環境ごとに別のシステム ID を持つ"
    "（list_environments で確認し、compare_environments で比較できる）。"
    "パラメータと構成項目は「レイヤーキー.キー」の参照名で識別する。"
    "値や採用案を変えると承認は下書きに戻り、下流のパラメータに要再計算が付くので、"
    "recalc → apply_proposals で反映する。過去の内容は get_timeline / get_state_at / diff_history で調べ、"
    "restore_revision で戻せる。"
)


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None, http_status: int = 200):
        super().__init__(message)
        self.code, self.message, self.data, self.http_status = code, message, data, http_status


def error_response(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _decode_header(value: str) -> str:
    """``=?base64?...?=`` 形式のヘッダー値を復号する。"""
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise McpError(HEADER_MISMATCH, "Header mismatch: Mcp-Name is not valid Base64", http_status=400) from None
    return value


def _get(headers: Mapping[str, str], name: str) -> str | None:
    v = headers.get(name)
    if v is None:  # dict の場合に備えて大文字小文字を無視して探す
        low = name.lower()
        v = next((val for k, val in headers.items() if k.lower() == low), None)
    return v.strip() if isinstance(v, str) else None


def tool_definition(tool: Any) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
        "annotations": {
            "readOnlyHint": tool.read_only,
            "destructiveHint": tool.destructive,
            "idempotentHint": tool.read_only or tool.method == "PUT",
            "openWorldHint": False,
        },
    }


class McpServer:
    def __init__(self, client: ApiClient, version: str = "0.0.0", default_actor: str = "mcp"):
        self.client = client
        self.server_info = {"name": "reqviz", "title": "reqviz 構成管理", "version": version}
        self.default_actor = default_actor

    # ------------------------------------------------------------ 入口

    def handle(self, msg: Any, headers: Mapping[str, str] | None = None, actor: str | None = None) -> tuple[int, dict[str, Any] | None]:
        """JSON-RPC メッセージ 1 件を処理し、(HTTP ステータス, 応答) を返す。

        ``headers`` は Streamable HTTP のときだけ渡す（stdio では None）。通知への応答は None。
        """
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            return 400, error_response(msg_id, INVALID_REQUEST, "Invalid Request: a single JSON-RPC 2.0 request or notification is required")
        if "id" not in msg:
            return 202, None  # 通知（notifications/initialized, notifications/cancelled など）は受理するだけ
        msg_id = msg["id"]
        try:
            params = msg.get("params")
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise McpError(INVALID_PARAMS, "Invalid params: params must be an object", http_status=400)
            result = self._dispatch(msg["method"], params, headers, actor or self.default_actor)
            return 200, {"jsonrpc": "2.0", "id": msg_id, "result": result}
        except McpError as e:
            return e.http_status, error_response(msg_id, e.code, e.message, e.data)
        except Exception as e:  # pragma: no cover - 想定外
            return 500, error_response(msg_id, INTERNAL_ERROR, f"Internal error: {type(e).__name__}: {e}")

    # ------------------------------------------------------------ 版の判定

    def _era(self, method: str, params: dict[str, Any], headers: Mapping[str, str] | None) -> str | None:
        """リクエストの版を検証し、新方式ならその版を、旧方式なら None を返す。"""
        meta = params.get("_meta")
        requested = meta.get(META_VERSION) if isinstance(meta, dict) else None
        header_version = _get(headers, "MCP-Protocol-Version") if headers is not None else None

        if requested is None:
            if header_version in MODERN_VERSIONS:
                raise McpError(
                    INVALID_PARAMS, f"Invalid params: _meta[\"{META_VERSION}\"] is required for protocol version {header_version}",
                    http_status=400,
                )
            if header_version is not None and header_version not in LEGACY_VERSIONS:
                raise self._unsupported(header_version)
            return None  # 旧方式（initialize 済みのクライアント）

        if not isinstance(requested, str) or requested not in MODERN_VERSIONS:
            raise self._unsupported(str(requested))
        if headers is not None:
            if header_version is None:
                raise McpError(HEADER_MISMATCH, "Header mismatch: MCP-Protocol-Version header is required", http_status=400)
            if header_version != requested:
                raise McpError(
                    HEADER_MISMATCH,
                    f"Header mismatch: MCP-Protocol-Version header value '{header_version}' does not match body value '{requested}'",
                    http_status=400,
                )
            self._check_header(headers, "Mcp-Method", method)
            if method == "tools/call":
                self._check_header(headers, "Mcp-Name", params.get("name"))
        return requested

    @staticmethod
    def _unsupported(requested: str) -> McpError:
        return McpError(
            UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
            {"supported": list(SUPPORTED_VERSIONS), "requested": requested}, http_status=400,
        )

    @staticmethod
    def _check_header(headers: Mapping[str, str], name: str, body_value: Any) -> None:
        raw = _get(headers, name)
        if raw is None:
            raise McpError(HEADER_MISMATCH, f"Header mismatch: {name} header is required", http_status=400)
        value = _decode_header(raw)
        if value != body_value:
            raise McpError(
                HEADER_MISMATCH, f"Header mismatch: {name} header value '{value}' does not match body value '{body_value}'",
                http_status=400,
            )

    # ------------------------------------------------------------ メソッド

    def _dispatch(self, method: str, params: dict[str, Any], headers: Mapping[str, str] | None, actor: str) -> dict[str, Any]:
        if method == "initialize":
            return self._initialize(params)
        modern = self._era(method, params, headers) is not None

        if method == "server/discover":
            result: dict[str, Any] = {
                "supportedVersions": list(SUPPORTED_VERSIONS),
                "capabilities": {"tools": {}},
                "instructions": INSTRUCTIONS,
                "ttlMs": TOOLS_TTL_MS,
                "cacheScope": "public",
            }
        elif method == "tools/list":
            result = {"tools": [tool_definition(t) for t in TOOLS]}
            if modern:
                result.update(ttlMs=TOOLS_TTL_MS, cacheScope="public")
        elif method == "tools/call":
            result = self._call_tool(params, actor, modern)
        elif method == "ping" and not modern:
            result = {}
        else:
            raise McpError(METHOD_NOT_FOUND, f"Method not found: {method}", http_status=404 if modern else 200)

        if modern:
            result["resultType"] = "complete"
            result["_meta"] = {META_SERVER_INFO: self.server_info}
        return result

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": self.server_info,
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, params: dict[str, Any], actor: str, modern: bool) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str) or name not in TOOLS_BY_NAME:
            raise McpError(INVALID_PARAMS, f"Unknown tool: {name}", http_status=400 if modern else 200)
        args = params.get("arguments")
        if args is not None and not isinstance(args, dict):
            raise McpError(INVALID_PARAMS, "Invalid params: arguments must be an object", http_status=400 if modern else 200)
        try:
            res = call_tool(self.client, name, args, actor)
        except ToolError as e:
            # 引数の誤りはモデルが自己修正できるよう、ツールの実行エラーとして返す
            return {"content": [{"type": "text", "text": str(e)}], "isError": True}
        if not res.ok:
            return {"content": [{"type": "text", "text": f"{res.error_message}（HTTP {res.status}）"}], "isError": True}
        data = res.data
        text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
        if not isinstance(data, str):
            # 旧方式では structuredContent はオブジェクトに限られる
            result["structuredContent"] = data if modern or isinstance(data, dict) else {"result": data}
        return result
