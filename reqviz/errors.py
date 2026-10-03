"""アプリケーション例外（HTTP ステータスに対応）。"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class BadRequest(AppError):
    status = 400
    code = "bad_request"


class NotFound(AppError):
    status = 404
    code = "not_found"


class Conflict(AppError):
    """楽観ロックの不一致、または一意制約違反。"""

    status = 409
    code = "conflict"


class Invalid(AppError):
    """入力値・業務ルールの検証エラー。"""

    status = 422
    code = "invalid"
