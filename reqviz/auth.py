"""ログイン認証。パスワード、セッション、API トークン、アカウント管理。

- パスワードは PBKDF2-HMAC-SHA256 でハッシュ化して保存する
- 画面はセッション Cookie（HttpOnly, SameSite=Strict）+ CSRF トークンで認証する
- REST API・MCP・A2A をプログラムから使う場合は、個人の API トークン（Authorization: Bearer）で認証する
- アカウントは管理者だけが作成できる。初期パスワードは最初のログイン時に変更させる
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from typing import Any

from .context import Ctx
from .db import now, transaction
from .errors import AppError, Invalid, NotFound
from .repo import history

COOKIE_NAME = "reqviz_session"
TOKEN_PREFIX = "rvz_"
ROLES = ("designer", "approver", "admin")
ROLE_LABELS = {"designer": "設計者", "approver": "承認者", "admin": "管理者"}
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 256
LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{1,63}$")
MAX_FAILURES = 5
LOCK_SECONDS = 300


class Unauthorized(AppError):
    status = 401
    code = "unauthorized"


class Forbidden(AppError):
    status = 403
    code = "forbidden"


class TooManyRequests(AppError):
    status = 429
    code = "too_many_requests"


@dataclass
class Principal:
    """認証済みの利用者。"""

    login: str
    role: str = "admin"
    user_id: int | None = None
    display_name: str = ""
    must_change_password: bool = False
    via: str = "internal"  # session / token / internal
    csrf_token: str | None = None
    session_id: int | None = None

    def public(self) -> dict[str, Any]:
        return {
            "id": self.user_id, "login": self.login, "display_name": self.display_name or self.login,
            "role": self.role, "must_change_password": self.must_change_password,
        }


# ---------------------------------------------------------------- パスワード

def hash_password(password: str, iterations: int | None = None) -> str:
    iterations = iterations or PBKDF2_ITERATIONS
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    try:
        algo, iterations, salt, digest = (stored or "").split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(calc, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def check_password_policy(login: str, password: Any) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise Invalid(f"パスワードは {MIN_PASSWORD_LENGTH} 文字以上で指定してください")
    if len(password) > MAX_PASSWORD_LENGTH:
        raise Invalid(f"パスワードが長すぎます（最大 {MAX_PASSWORD_LENGTH} 文字）")
    if password.lower() == login.lower():
        raise Invalid("ID と同じパスワードは使えません")
    return password


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- ログイン試行の制限（プロセス内）

_failures: dict[str, tuple[int, float]] = {}
_failures_lock = threading.Lock()


def _check_locked(login: str) -> None:
    with _failures_lock:
        count, until = _failures.get(login, (0, 0.0))
        if count >= MAX_FAILURES and time.monotonic() < until:
            raise TooManyRequests(f"ログインの失敗が続いたため、一時的に受け付けていません。{LOCK_SECONDS // 60} 分ほど待ってからやり直してください")
        if count >= MAX_FAILURES:
            _failures.pop(login, None)


def _record_failure(login: str) -> None:
    with _failures_lock:
        count, _ = _failures.get(login, (0, 0.0))
        _failures[login] = (count + 1, time.monotonic() + LOCK_SECONDS)
        if len(_failures) > 10_000:  # 際限なく増えないようにする
            _failures.clear()


def reset_failures() -> None:
    with _failures_lock:
        _failures.clear()


# ---------------------------------------------------------------- アカウント

def user_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d.pop("password_hash", None)
    d["disabled"] = bool(d["disabled"])
    d["must_change_password"] = bool(d["must_change_password"])
    d["display_name"] = d["display_name"] or d["login"]
    return d


def list_users(ctx: Ctx) -> list[dict[str, Any]]:
    return [user_dict(r) for r in ctx.conn.execute("SELECT * FROM users ORDER BY login")]


def get_user(ctx: Ctx, user_id: int) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if r is None:
        raise NotFound(f"アカウントが見つかりません: {user_id}")
    return user_dict(r)


def _validate_login(value: Any) -> str:
    if not isinstance(value, str) or not LOGIN_RE.match(value.strip()):
        raise Invalid("ID は英数字で始まる 2〜64 文字（英数字 . _ -）で指定してください")
    return value.strip()


def _validate_role(value: Any) -> str:
    if value not in ROLES:
        raise Invalid(f"ロールは {' / '.join(ROLES)} のいずれかで指定してください: {value!r}")
    return value


def _active_admins(conn: sqlite3.Connection, exclude_id: int | None = None) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0 AND id IS NOT ?", (exclude_id,)
    ).fetchone()[0]


def create_user(ctx: Ctx, data: dict[str, Any]) -> dict[str, Any]:
    """アカウントを作成する。初期パスワードは、本人が最初のログイン時に変更する。"""
    login = _validate_login(data.get("login"))
    role = _validate_role(data.get("role") or "designer")
    password = check_password_policy(login, data.get("password"))
    display = data.get("display_name") or ""
    if not isinstance(display, str) or len(display) > 64:
        raise Invalid("表示名は 64 文字以内の文字列で指定してください")
    with transaction(ctx.conn):
        if ctx.conn.execute("SELECT 1 FROM users WHERE login = ? COLLATE NOCASE", (login,)).fetchone():
            raise Invalid(f"ID {login} は既に使われています")
        ts = now()
        cur = ctx.conn.execute(
            "INSERT INTO users (login, display_name, role, password_hash, must_change_password, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (login, display.strip(), role, hash_password(password), 0 if data.get("must_change_password") is False else 1, ts, ts),
        )
        history.audit(ctx, None, "user.create", "user", login, {"role": role})
    return get_user(ctx, cur.lastrowid)


def update_user(ctx: Ctx, user_id: int, data: dict[str, Any], acting: Principal | None = None) -> dict[str, Any]:
    """表示名・ロール・無効化・パスワードの再設定（管理者用）。"""
    before = get_user(ctx, user_id)
    fields: dict[str, Any] = {}
    if "display_name" in data:
        display = data.get("display_name") or ""
        if not isinstance(display, str) or len(display) > 64:
            raise Invalid("表示名は 64 文字以内の文字列で指定してください")
        fields["display_name"] = display.strip()
    if "role" in data:
        fields["role"] = _validate_role(data["role"])
    if "disabled" in data:
        fields["disabled"] = 1 if data["disabled"] else 0
    if data.get("password"):
        fields["password_hash"] = hash_password(check_password_policy(before["login"], data["password"]))
        fields["must_change_password"] = 1
    if acting and acting.user_id == user_id and (fields.get("disabled") or fields.get("role", "admin") != "admin"):
        raise Invalid("自分自身を無効にしたり、管理者から外したりすることはできません")
    loses_admin = before["role"] == "admin" and not before["disabled"] and (
        fields.get("disabled") or fields.get("role", "admin") != "admin"
    )
    with transaction(ctx.conn):
        if loses_admin and _active_admins(ctx.conn, exclude_id=user_id) == 0:
            raise Invalid("有効な管理者が 1 人もいなくなるため、変更できません")
        if fields:
            fields["updated_at"] = now()
            sets = ", ".join(f"{k} = ?" for k in fields)
            ctx.conn.execute(f"UPDATE users SET {sets} WHERE id = ?", [*fields.values(), user_id])
        if fields.get("disabled") or "password_hash" in fields:
            # 無効化・パスワード再設定では、ログイン中のセッションを切る（無効化では API トークンも止まる）
            ctx.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        changed = sorted(k for k in fields if k not in ("updated_at", "password_hash", "must_change_password"))
        if "password_hash" in fields:
            changed.append("password")
        history.audit(ctx, None, "user.update", "user", before["login"], {"changed": changed})
    return get_user(ctx, user_id)


def delete_user(ctx: Ctx, user_id: int, acting: Principal | None = None) -> None:
    user = get_user(ctx, user_id)
    if acting and acting.user_id == user_id:
        raise Invalid("自分自身のアカウントは削除できません")
    with transaction(ctx.conn):
        if user["role"] == "admin" and not user["disabled"] and _active_admins(ctx.conn, exclude_id=user_id) == 0:
            raise Invalid("有効な管理者が 1 人もいなくなるため、削除できません")
        ctx.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        history.audit(ctx, None, "user.delete", "user", user["login"])


def reset_password(ctx: Ctx, login_id: str, password: str, revoke_tokens: bool = False) -> dict[str, Any]:
    """ID を指定してパスワードを再設定する（コマンドライン用。管理者の画面操作と同じ扱い）。

    本人は次のログイン時にパスワードを変更する。ログイン中のセッションは切れる。
    """
    row = ctx.conn.execute("SELECT id FROM users WHERE login = ? COLLATE NOCASE", (login_id.strip(),)).fetchone()
    if row is None:
        raise NotFound(f"アカウントが見つかりません: {login_id}")
    with transaction(ctx.conn):
        user = update_user(ctx, row["id"], {"password": password})
        if revoke_tokens:
            ctx.conn.execute("DELETE FROM api_tokens WHERE user_id = ?", (row["id"],))
            history.audit(ctx, None, "auth.token_revoke_all", "user", user["login"])
    return user


def ensure_admin(conn: sqlite3.Connection, login: str, password: str | None) -> tuple[str, str] | None:
    """アカウントが 1 件もなければ最初の管理者を作る。パスワードを自動生成した場合は (ID, パスワード) を返す。"""
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        return None
    generated = not password
    password = password or secrets.token_urlsafe(12)
    ctx = Ctx(conn, "system")
    create_user(ctx, {"login": login, "role": "admin", "password": password, "display_name": "管理者", "must_change_password": generated})
    return (login, password) if generated else None


# ---------------------------------------------------------------- ログイン・セッション

def _principal(row: Any, via: str, csrf: str | None = None, session_id: int | None = None) -> Principal:
    return Principal(
        login=row["login"], role=row["role"], user_id=row["user_id"], display_name=row["display_name"] or "",
        must_change_password=bool(row["must_change_password"]), via=via, csrf_token=csrf, session_id=session_id,
    )


def _create_session(conn: sqlite3.Connection, user_id: int, hours: float) -> tuple[str, str, int]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now(),))
    cur = conn.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
        (_sha256(token), user_id, csrf, now(), expires),
    )
    return token, csrf, cur.lastrowid


def login(ctx: Ctx, login_id: Any, password: Any, session_hours: float = 12) -> tuple[str, Principal]:
    """ID とパスワードを確認し、(セッショントークン, 利用者) を返す。"""
    if not isinstance(login_id, str) or not isinstance(password, str) or not login_id.strip() or not password:
        raise Invalid("ID とパスワードを入力してください")
    login_id = login_id.strip()[:64]
    key = login_id.lower()
    _check_locked(key)
    row = ctx.conn.execute("SELECT * FROM users WHERE login = ? COLLATE NOCASE", (login_id,)).fetchone()
    # 存在しない ID でも同じ時間がかかるよう、ダミーのハッシュと照合する
    ok = verify_password(password, row["password_hash"] if row else _dummy_hash()) and row is not None and not row["disabled"]
    if not ok:
        _record_failure(key)
        history.audit(Ctx(ctx.conn, login_id), None, "auth.login_failed", "user", login_id)
        raise Unauthorized("ID またはパスワードが違います")
    with _failures_lock:
        _failures.pop(key, None)
    with transaction(ctx.conn):
        token, csrf, sid = _create_session(ctx.conn, row["id"], session_hours)
        history.audit(Ctx(ctx.conn, row["login"]), None, "auth.login", "user", row["login"])
    d = {**dict(row), "user_id": row["id"]}
    return token, _principal(d, "session", csrf, sid)


_dummy: list[str] = []


def _dummy_hash() -> str:
    if not _dummy:
        _dummy.append(hash_password(secrets.token_urlsafe(16)))
    return _dummy[0]


def logout(ctx: Ctx, principal: Principal | None) -> None:
    if principal and principal.session_id:
        ctx.conn.execute("DELETE FROM sessions WHERE id = ?", (principal.session_id,))
        history.audit(ctx, None, "auth.logout", "user", principal.login)


def session_cookie(token: str, hours: float, secure: bool) -> str:
    return (
        f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={int(hours * 3600)}"
        + ("; Secure" if secure else "")
    )


def clear_cookie(secure: bool) -> str:
    return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0" + ("; Secure" if secure else "")


_USER_COLS = "u.id AS user_id, u.login, u.display_name, u.role, u.must_change_password"


def authenticate(conn: sqlite3.Connection, headers: Any, allow_cookie: bool = True) -> Principal | None:
    """リクエストのヘッダーから利用者を特定する。Bearer トークンを優先し、なければセッション Cookie を見る。"""
    authz = (headers.get("Authorization") or "").strip()
    if authz[:7].lower() == "bearer ":
        raw = authz[7:].strip()
        row = conn.execute(
            f"SELECT t.id AS token_id, {_USER_COLS} FROM api_tokens t JOIN users u ON u.id = t.user_id"
            " WHERE t.token_hash = ? AND u.disabled = 0", (_sha256(raw),),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (now(), row["token_id"]))
        return _principal(row, "token")
    if not allow_cookie:
        return None
    try:
        morsel = SimpleCookie(headers.get("Cookie") or "").get(COOKIE_NAME)
    except CookieError:
        return None
    if morsel is None or not morsel.value:
        return None
    row = conn.execute(
        f"SELECT s.id AS session_id, s.csrf_token, {_USER_COLS} FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0", (_sha256(morsel.value), now()),
    ).fetchone()
    return _principal(row, "session", row["csrf_token"], row["session_id"]) if row else None


def lookup(conn: sqlite3.Connection, login_id: str) -> Principal | None:
    """ID から利用者を引く（認証済みの入口から内部で API を呼ぶとき用）。"""
    row = conn.execute(
        f"SELECT {_USER_COLS} FROM users u WHERE u.login = ? AND u.disabled = 0", (login_id,)
    ).fetchone()
    return _principal(row, "internal") if row else None


def change_password(ctx: Ctx, principal: Principal, current: Any, new: Any) -> None:
    """本人によるパスワード変更。他の端末のセッションは切る。"""
    row = ctx.conn.execute("SELECT * FROM users WHERE id = ?", (principal.user_id,)).fetchone()
    if row is None or not isinstance(current, str) or not verify_password(current, row["password_hash"]):
        raise Invalid("現在のパスワードが違います")
    new = check_password_policy(row["login"], new)
    if new == current:
        raise Invalid("現在と異なるパスワードを指定してください")
    with transaction(ctx.conn):
        ctx.conn.execute(
            "UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? WHERE id = ?",
            (hash_password(new), now(), row["id"]),
        )
        ctx.conn.execute("DELETE FROM sessions WHERE user_id = ? AND id IS NOT ?", (row["id"], principal.session_id))
        history.audit(ctx, None, "auth.password_change", "user", row["login"])


# ---------------------------------------------------------------- API トークン

def list_tokens(ctx: Ctx, user_id: int) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        "SELECT id, name, prefix, created_at, last_used_at FROM api_tokens WHERE user_id = ? ORDER BY id", (user_id,)
    )
    return [dict(r) for r in rows]


def create_token(ctx: Ctx, principal: Principal, name: Any) -> dict[str, Any]:
    """API トークンを発行する。平文（token）を返すのはこのときだけ。"""
    if not isinstance(name, str) or not name.strip() or len(name) > 64:
        raise Invalid("トークンの名前を 64 文字以内で指定してください")
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    with transaction(ctx.conn):
        cur = ctx.conn.execute(
            "INSERT INTO api_tokens (user_id, name, token_hash, prefix, created_at) VALUES (?, ?, ?, ?, ?)",
            (principal.user_id, name.strip(), _sha256(raw), raw[:10], now()),
        )
        history.audit(ctx, None, "auth.token_create", "user", principal.login, {"name": name.strip()})
    return {"id": cur.lastrowid, "name": name.strip(), "prefix": raw[:10], "token": raw}


def delete_token(ctx: Ctx, principal: Principal, token_id: int) -> None:
    cur = ctx.conn.execute("DELETE FROM api_tokens WHERE id = ? AND user_id = ?", (token_id, principal.user_id))
    if cur.rowcount == 0:
        raise NotFound(f"トークンが見つかりません: {token_id}")
    history.audit(ctx, None, "auth.token_delete", "user", principal.login, {"id": token_id})
