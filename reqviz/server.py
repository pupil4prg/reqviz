"""HTTP サーバー（ThreadingHTTPServer）と静的ファイル配信。"""

from __future__ import annotations

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

from . import db
from .ai import jobs
from .api.routes import content_disposition, router
from .config import Config, LLMConfig
from .context import Ctx
from .errors import AppError, BadRequest
from .router import Request, Response
from .workflow import templates

STATIC_DIR = Path(__file__).parent / "static"
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
    def __init__(self, config: Config, run_ai_in_thread: bool = True):
        self.config = config
        self.run_ai_in_thread = run_ai_in_thread
        conn = db.connect(config.db_path)
        try:
            db.migrate(conn)
            if config.templates_dir.is_dir():
                templates.seed_bundled(Ctx(conn, "system"), config.templates_dir)
        finally:
            conn.close()

    def start_ai_run(self, actor: str, run_id: int, cfg: LLMConfig) -> None:
        if self.run_ai_in_thread:
            jobs.start_in_thread(self.config.db_path, actor, run_id, cfg)

    def handle(self, method: str, raw_path: str, headers: Any, body: bytes) -> Response:
        parts = urlsplit(raw_path)
        path = unquote(parts.path)
        if not path.startswith("/api/"):
            return self.static(path) if method in ("GET", "HEAD") else self.error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "許可されていないメソッドです")

        m = router.match(method, path)
        if m is None:
            return self.error(HTTPStatus.NOT_FOUND, "not_found", f"API が見つかりません: {path}")
        if m == "method_not_allowed":
            return self.error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "許可されていないメソッドです")
        handler, params = m  # type: ignore[misc]

        conn = db.connect(self.config.db_path)
        try:
            actor = (headers.get("X-Reqviz-User") or "").strip() or self.config.default_user
            ctx = Ctx(conn, unquote(actor)[:64])
            parsed: Any = None
            if body:
                try:
                    parsed = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    raise BadRequest("リクエスト本文が JSON ではありません") from None
            req = Request(method, path, ctx, params, dict(parse_qsl(parts.query)), parsed, self)
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
    server_version = "reqviz/0.1"
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
