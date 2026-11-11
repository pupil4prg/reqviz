"""HTTP サーバー（ThreadingHTTPServer）と静的ファイル配信。"""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import sqlite3
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from . import __version__, auth, db
from .a2a.server import A2AServer, agent_card
from .agent.tools import InProcessClient
from .ai import jobs
from .api.routes import content_disposition, router
from .config import Config, LLMConfig
from .context import Ctx
from .errors import AppError, BadRequest
from .router import Request, Response
from .mcp.protocol import PARSE_ERROR, McpServer, error_response
from .workflow import templates

STATIC_DIR = Path(__file__).parent / "static"
AGENT_CARD_PATH = "/.well-known/agent-card.json"
PUBLIC_API = ("/api/auth/login", "/api/auth/logout", "/api/auth/me")  # ログイン前でも呼べる API
MAX_BODY = 10 * 1024 * 1024
log = logging.getLogger("reqviz")

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


class App:
    version = __version__

    def __init__(self, config: Config, run_ai_in_thread: bool = True):
        self.config = config
        self.run_ai_in_thread = run_ai_in_thread
        client = InProcessClient(self)
        self.mcp = McpServer(client, __version__)
        self.a2a = A2AServer(
            config.db_path, client, lambda ctx: jobs.effective_llm_config(ctx, config.llm), __version__,
            run_in_thread=run_ai_in_thread,
        )
        self.bootstrap_admin: tuple[str, str] | None = None
        conn = db.connect(config.db_path)
        try:
            db.migrate(conn)
            if config.auth_enabled:
                self.bootstrap_admin = auth.ensure_admin(conn, config.admin_user, config.admin_password)
            if config.templates_dir.is_dir():
                templates.seed_bundled(Ctx(conn, "system"), config.templates_dir)
        finally:
            conn.close()

    def start_ai_run(self, actor: str, run_id: int, cfg: LLMConfig) -> None:
        if self.run_ai_in_thread:
            jobs.start_in_thread(self.config.db_path, actor, run_id, cfg)

    def internal_principal(self, actor: str | None) -> auth.Principal:
        """認証済みの入口（MCP・A2A）から内部で API を呼ぶときの利用者。ロールは DB から引き直す。"""
        actor = actor or self.config.default_user
        if not self.config.auth_enabled:
            return auth.Principal(actor)
        conn = db.connect(self.config.db_path)
        try:
            found = auth.lookup(conn, actor)
        finally:
            conn.close()
        if found is None:
            raise auth.Unauthorized(f"アカウントが無効です: {actor}")
        return found

    def handle(self, method: str, raw_path: str, headers: Any, body: bytes, principal: auth.Principal | None = None) -> Response:
        """リクエストを処理する。principal を渡すのは、認証済みの入口から内部で呼ぶ場合だけ。"""
        parts = urlsplit(raw_path)
        path = unquote(parts.path)
        if self.config.agent_endpoints and path in ("/mcp", "/a2a", AGENT_CARD_PATH):
            return self.agent_endpoint(method, path, headers, body, dict(parse_qsl(parts.query)))
        if path != "/api" and not path.startswith("/api/"):
            return self.static(path) if method in ("GET", "HEAD") else self.error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "許可されていないメソッドです")

        m = router.match(method, path)
        if m is None:
            return self.error(HTTPStatus.NOT_FOUND, "not_found", f"API が見つかりません: {path}")
        if m == "method_not_allowed":
            return self.error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "許可されていないメソッドです")
        handler, params = m  # type: ignore[misc]

        conn = db.connect(self.config.db_path)
        try:
            if principal is not None:
                actor = principal.login
            elif self.config.auth_enabled:
                principal = auth.authenticate(conn, headers)
                self._authorize(method, path, headers, principal)
                actor = principal.login if principal else "anonymous"
            else:
                actor = unquote((headers.get("X-Reqviz-User") or "").strip())[:64] or self.config.default_user
            ctx = Ctx(conn, actor)
            parsed: Any = None
            if body:
                try:
                    parsed = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise BadRequest("リクエスト本文が JSON ではありません") from None
            req = Request(method, path, ctx, params, dict(parse_qsl(parts.query)), parsed, self, principal)
            result = handler(req)
            if isinstance(result, Response):
                return result
            return Response(json.dumps(result, ensure_ascii=False, default=str).encode("utf-8"))
        except AppError as e:
            return Response(json.dumps(e.to_dict(), ensure_ascii=False).encode("utf-8"), status=e.status)
        except sqlite3.IntegrityError as e:
            return self.error(HTTPStatus.CONFLICT, "integrity", f"データの整合性エラー: {e}")
        except Exception as e:  # pragma: no cover - 想定外のエラー
            log.error("unhandled error: %s\n%s", e, traceback.format_exc())
            return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal", f"内部エラー: {type(e).__name__}: {e}")
        finally:
            conn.close()

    @staticmethod
    def _authorize(method: str, path: str, headers: Any, principal: auth.Principal | None) -> None:
        """REST API の入口での確認（未ログイン、CSRF、初期パスワードの変更待ち）。"""
        if path in PUBLIC_API:
            return
        if principal is None:
            raise auth.Unauthorized("ログインが必要です")
        if principal.via == "session" and method not in ("GET", "HEAD"):
            sent = (headers.get("X-CSRF-Token") or "").strip()
            if not sent or not hmac.compare_digest(sent, principal.csrf_token or ""):
                raise auth.Forbidden("CSRF トークンが一致しません。画面を再読み込みしてください")
        if principal.must_change_password and not path.startswith("/api/auth/"):
            err = auth.Forbidden("初期パスワードを変更してください")
            err.code = "password_change_required"
            raise err

    # ------------------------------------------------------------ MCP / A2A

    def _actor(self, headers: Any, fallback: str) -> str:
        return unquote((headers.get("X-Reqviz-User") or "").strip())[:64] or fallback

    def _agent_actor(self, headers: Any, fallback: str) -> str | None:
        """MCP・A2A の操作者。認証が有効なら API トークンが必須で、なければ None。"""
        if not self.config.auth_enabled:
            return self._actor(headers, fallback)
        conn = db.connect(self.config.db_path)
        try:
            p = auth.authenticate(conn, headers, allow_cookie=False)
        finally:
            conn.close()
        return p.login if p and not p.must_change_password else None

    def _origin_allowed(self, headers: Any) -> bool:
        """DNS リバインディング対策。Origin があるときは、自分自身か許可リストのものだけを通す。"""
        origin = (headers.get("Origin") or "").strip().rstrip("/")
        if not origin:
            return True
        if origin in self.config.allowed_origins:
            return True
        host = (headers.get("Host") or "").strip()
        return bool(host) and urlsplit(origin).netloc == host

    def base_url(self, headers: Any) -> str:
        if self.config.public_url:
            return self.config.public_url
        host = (headers.get("Host") or "").strip() or f"{self.config.host}:{self.config.port}"
        return f"http://{host}"

    def agent_endpoint(self, method: str, path: str, headers: Any, body: bytes, query: dict[str, str]) -> Response:
        def as_json(obj: Any, status: int = 200) -> Response:
            return Response(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), status=status)

        if path == AGENT_CARD_PATH:
            if method not in ("GET", "HEAD"):
                return Response(b"", status=405, headers={"Allow": "GET"})
            return as_json(agent_card(self.base_url(headers), __version__, self.config.auth_enabled))
        if not self._origin_allowed(headers):
            return as_json(error_response(None, -32600, "Forbidden: Origin is not allowed"), 403)
        if method != "POST":
            return Response(b"", status=405, headers={"Allow": "POST"})
        actor = self._agent_actor(headers, "a2a" if path == "/a2a" else "mcp")
        if actor is None:
            res = as_json(error_response(None, -32600, "Unauthorized: a valid API token is required (Authorization: Bearer <token>)"), 401)
            res.headers["WWW-Authenticate"] = 'Bearer realm="reqviz"'
            return res
        if path == "/a2a":
            return as_json(self.a2a.handle(body, headers, query, actor))
        try:
            msg = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return as_json(error_response(None, PARSE_ERROR, "Parse error"), 400)
        status, res = self.mcp.handle(msg, headers, actor)
        return Response(b"", status=status) if res is None else as_json(res, status)

    def static(self, path: str) -> Response:
        if path in ("", "/"):
            path = "/index.html"
        target = (STATIC_DIR / path.lstrip("/")).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            return self.error(HTTPStatus.NOT_FOUND, "not_found", "ファイルが見つかりません")
        ctype = CONTENT_TYPES.get(target.suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return Response(target.read_bytes(), ctype)

    @staticmethod
    def error(status: int, code: str, message: str) -> Response:
        body = {"error": {"code": code, "message": message, "details": None}}
        return Response(json.dumps(body, ensure_ascii=False).encode("utf-8"), status=int(status))


class Handler(BaseHTTPRequestHandler):
    server_version = f"reqviz/{__version__}"
    app: App  # make_server で設定

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            res = App.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "too_large", "リクエストが大きすぎます")
        else:
            body = self.rfile.read(length) if length else b""
            res = self.app.handle(self.command, self.path, self.headers, body)
        self.send_response(res.status)
        self.send_header("Content-Type", res.content_type)
        self.send_header("Content-Length", str(len(res.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if res.filename:
            self.send_header("Content-Disposition", content_disposition(res.filename))
        for k, v in res.headers.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(res.body)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = _dispatch

    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)


def make_server(config: Config, run_ai_in_thread: bool = True) -> ThreadingHTTPServer:
    app = App(config, run_ai_in_thread)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    server.app = app  # type: ignore[attr-defined]
    return server
