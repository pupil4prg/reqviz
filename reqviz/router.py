"""最小限のルーター。パスパターン ``/api/params/{id:int}`` 形式をサポートする。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .context import Ctx
from .errors import BadRequest


@dataclass
class Request:
    method: str
    path: str
    ctx: Ctx
    params: dict[str, Any] = field(default_factory=dict)
    query: dict[str, str] = field(default_factory=dict)
    body: Any = None
    app: Any = None

    def json(self) -> dict[str, Any]:
        if self.body is None:
            return {}
        if not isinstance(self.body, dict):
            raise BadRequest("JSON オブジェクトを送信してください")
        return self.body


@dataclass
class Response:
    body: bytes
    content_type: str = "application/json; charset=utf-8"
    status: int = 200
    filename: str | None = None


Handler = Callable[[Request], Any]

_PARAM_RE = re.compile(r"\{(\w+)(?::(int|str))?\}")


class Router:
    def __init__(self) -> None:
        self.routes: list[tuple[str, re.Pattern[str], dict[str, str], Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        types: dict[str, str] = {}

        def repl(m: re.Match[str]) -> str:
            types[m.group(1)] = m.group(2) or "str"
            return rf"(?P<{m.group(1)}>\d+)" if m.group(2) == "int" else rf"(?P<{m.group(1)}>[^/]+)"

        regex = re.compile("^" + _PARAM_RE.sub(repl, pattern) + "$")
        self.routes.append((method.upper(), regex, types, handler))

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self.add(method, pattern, fn)
            return fn

        return deco

    def match(self, method: str, path: str) -> tuple[Handler, dict[str, Any]] | None | str:
        """一致すれば (handler, params)。パスのみ一致なら 'method_not_allowed'。"""
        path_matched = False
        for m, regex, types, handler in self.routes:
            mm = regex.match(path)
            if not mm:
                continue
            path_matched = True
            if m != method.upper():
                continue
            params: dict[str, Any] = {
                k: int(v) if types.get(k) == "int" else v for k, v in mm.groupdict().items()
            }
            return handler, params
        return "method_not_allowed" if path_matched else None
