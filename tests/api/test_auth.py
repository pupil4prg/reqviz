import json
import threading
import urllib.error
import urllib.request

import pytest

from reqviz import auth
from reqviz.config import Config, LLMConfig
from reqviz.server import make_server

from ..conftest import ROOT


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    auth.reset_failures()


@pytest.fixture
def server(tmp_path):
    cfg = Config(host="127.0.0.1", port=0, db_path=tmp_path / "auth.db", templates_dir=ROOT / "templates",
                 llm=LLMConfig(), admin_user="root", admin_password="root-pass-1")
    srv = make_server(cfg, run_ai_in_thread=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


class Client:
    """Cookie と CSRF トークンを保持する、ブラウザ相当のクライアント。"""

    def __init__(self, base):
        self.base, self.cookie, self.csrf = base, None, None

    def call(self, method, path, body=None, headers=None, csrf=True):
        h = {"Content-Type": "application/json", **(headers or {})}
        if self.cookie:
            h["Cookie"] = self.cookie
        if csrf and self.csrf and method != "GET":
            h["X-CSRF-Token"] = self.csrf
        req = urllib.request.Request(self.base + path, data=None if body is None else json.dumps(body).encode(), method=method, headers=h)
        try:
            with urllib.request.urlopen(req) as res:
                status, raw, hdrs = res.status, res.read(), res.headers
        except urllib.error.HTTPError as e:
            status, raw, hdrs = e.code, e.read(), e.headers
        return status, (json.loads(raw) if raw else None), hdrs

    def login(self, login, password):
        st, res, hdrs = self.call("POST", "/api/auth/login", {"login": login, "password": password})
        if st == 200:
            self.cookie = hdrs["Set-Cookie"].split(";")[0]
            self.csrf = res["csrf_token"]
        return st, res, hdrs


def admin(server):
    c = Client(server)
    assert c.login("root", "root-pass-1")[0] == 200
    return c


def test_password_hashing():
    h = auth.hash_password("correct horse")
    assert h.startswith("pbkdf2_sha256$1000$") and h != auth.hash_password("correct horse")
    assert auth.verify_password("correct horse", h) and not auth.verify_password("wrong", h)
    assert not auth.verify_password("x", None) and not auth.verify_password("x", "garbage")


def test_login_required_and_session(server):
    anon = Client(server)
    with urllib.request.urlopen(server + "/") as res:
        assert res.status == 200
    st, me, _ = anon.call("GET", "/api/auth/me")
    assert st == 200 and me["auth_enabled"] is True and me["user"] is None
    for method, path in (("GET", "/api/systems"), ("GET", "/api/meta"), ("POST", "/api/systems"), ("GET", "/api/users"), ("GET", "/api")):
        st, err, _ = anon.call(method, path, {} if method == "POST" else None)
        assert st == 401 and err["error"]["code"] == "unauthorized"
    # X-Reqviz-User での自己申告は通らない
    assert anon.call("GET", "/api/systems", headers={"X-Reqviz-User": "root"})[0] == 401

    st, err, _ = anon.login("root", "wrong-password")
    assert st == 401 and "ID またはパスワード" in err["error"]["message"]
    assert anon.login("nobody", "root-pass-1")[1]["error"]["message"] == err["error"]["message"]  # ID の有無を漏らさない
    assert anon.login("", "")[0] == 422

    c = Client(server)
    st, res, hdrs = c.login("ROOT", "root-pass-1")  # ID は大文字小文字を区別しない
    cookie = hdrs["Set-Cookie"]
    assert st == 200 and res["user"] == {"id": 1, "login": "root", "display_name": "管理者", "role": "admin", "must_change_password": False}
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Secure" not in cookie
    st, me, _ = c.call("GET", "/api/auth/me")
    assert me["user"]["login"] == "root" and me["csrf_token"] == c.csrf
    assert c.call("GET", "/api/meta")[1]["user"] == "root"

    # 変更系は CSRF トークンが必要
    st, err, _ = c.call("POST", "/api/systems", {"name": "x"}, csrf=False)
    assert st == 403 and "CSRF" in err["error"]["message"]
    assert c.call("POST", "/api/systems", {"name": "x"}, headers={"X-CSRF-Token": "bad"}, csrf=False)[0] == 403
    st, s, _ = c.call("POST", "/api/systems", {"name": "x"}, headers={"X-Reqviz-User": "mallory"})
    assert st == 200
    audit = c.call("GET", "/api/audit")[1]
    assert [a["actor"] for a in audit if a["action"] == "system.create"] == ["root"]  # 操作者はログイン ID
    assert {"auth.login", "auth.login_failed"} <= {a["action"] for a in audit}

    st, _, hdrs = c.call("POST", "/api/auth/logout", {})
    assert st == 200 and "Max-Age=0" in hdrs["Set-Cookie"]
    assert c.call("GET", "/api/systems")[0] == 401  # 同じ Cookie はもう使えない


def test_lockout(server):
    c = Client(server)
    for _ in range(auth.MAX_FAILURES):
        assert c.login("root", "bad-password")[0] == 401
    st, err, _ = c.login("root", "root-pass-1")
    assert st == 429 and err["error"]["code"] == "too_many_requests"
    auth.reset_failures()
    assert c.login("root", "root-pass-1")[0] == 200


def test_admin_creates_accounts(server):
    a = admin(server)
    st, u, _ = a.call("POST", "/api/users", {"login": "sato", "display_name": "佐藤", "role": "designer", "password": "initial-pass"})
    assert st == 200 and u["must_change_password"] is True and "password_hash" not in u
    for bad in ({"login": "sato", "password": "another-pass"}, {"login": "x", "password": "long-enough"},
                {"login": "tanaka", "password": "short"}, {"login": "tanaka", "password": "tanaka"},
                {"login": "tanaka", "password": "long-enough", "role": "root"}):
        assert a.call("POST", "/api/users", bad)[0] == 422

    # 初期パスワードでログインすると、変更するまで他の操作はできない
    s = Client(server)
    st, res, _ = s.login("sato", "initial-pass")
    assert st == 200 and res["user"]["must_change_password"] is True
    st, err, _ = s.call("GET", "/api/systems")
    assert st == 403 and err["error"]["code"] == "password_change_required"
    assert s.call("POST", "/api/auth/password", {"current_password": "bad", "new_password": "new-password-1"})[0] == 422
    assert s.call("POST", "/api/auth/password", {"current_password": "initial-pass", "new_password": "short"})[0] == 422
    assert s.call("POST", "/api/auth/password", {"current_password": "initial-pass", "new_password": "initial-pass"})[0] == 422
    assert s.call("POST", "/api/auth/password", {"current_password": "initial-pass", "new_password": "new-password-1"})[0] == 200
    assert s.call("GET", "/api/systems")[0] == 200
    assert Client(server).login("sato", "initial-pass")[0] == 401

    # 一般ユーザーはアカウント管理も LLM 設定の変更もできない
    for method, path, body in (("GET", "/api/users", None), ("POST", "/api/users", {"login": "evil", "password": "long-enough"}),
                               ("PUT", f"/api/users/{u['id']}", {"role": "admin"}), ("DELETE", "/api/users/1", None),
                               ("PUT", "/api/settings/llm", {"base_url": "http://x/v1", "model": "m"})):
        st, err, _ = s.call(method, path, body)
        assert st == 403 and err["error"]["code"] == "forbidden", path

    # 管理者による変更・パスワード再設定・無効化
    assert a.call("PUT", f"/api/users/{u['id']}", {"role": "approver", "display_name": "佐藤（承認）"})[1]["role"] == "approver"
    st, r, _ = a.call("PUT", f"/api/users/{u['id']}", {"password": "reset-pass-99"})
    assert r["must_change_password"] is True
    assert s.call("GET", "/api/systems")[0] == 401  # 再設定でログイン中のセッションは切れる
    assert a.call("PUT", f"/api/users/{u['id']}", {"disabled": True})[1]["disabled"] is True
    assert Client(server).login("sato", "reset-pass-99")[0] == 401
    assert a.call("PUT", f"/api/users/{u['id']}", {"disabled": False})[0] == 200
    assert Client(server).login("sato", "reset-pass-99")[0] == 200

    # 最後の管理者は消せない・外せない。自分自身も不可
    for body in ({"role": "designer"}, {"disabled": True}):
        assert a.call("PUT", "/api/users/1", body)[0] == 422
    assert a.call("DELETE", "/api/users/1")[0] == 422
    assert a.call("DELETE", f"/api/users/{u['id']}")[1] == {"ok": True}
    assert a.call("GET", f"/api/users")[1][0]["login"] == "root" and len(a.call("GET", "/api/users")[1]) == 1
    assert {"user.create", "user.update", "user.delete", "auth.password_change"} <= {x["action"] for x in a.call("GET", "/api/audit")[1]}


def _user(server, a, login, role):
    a.call("POST", "/api/users", {"login": login, "role": role, "password": "initial-pass"})
    c = Client(server)
    c.login(login, "initial-pass")
    assert c.call("POST", "/api/auth/password", {"current_password": "initial-pass", "new_password": f"{login}-password"})[0] == 200
    return c


def test_roles_on_approval(server):
    a = admin(server)
    designer, approver = _user(server, a, "des", "designer"), _user(server, a, "apr", "approver")
    tpl = a.call("GET", "/api/templates")[1][0]
    sid = designer.call("POST", "/api/systems", {"name": "S", "template_id": tpl["id"]})[1]["id"]
    p = next(x for x in designer.call("GET", f"/api/systems/{sid}/params")[1] if x["ref"] == "biz.peak_tps")
    for action in ("skip_ai", "request_review"):
        assert designer.call("POST", f"/api/params/{p['id']}/transition", {"action": action})[0] == 200
    st, err, _ = designer.call("POST", f"/api/params/{p['id']}/transition", {"action": "approve"})
    assert st == 403 and "承認者" in err["error"]["message"]
    assert designer.call("POST", f"/api/systems/{sid}/transition", {"param_ids": [p["id"]], "action": "approve"})[0] == 403
    st, res, _ = approver.call("POST", f"/api/params/{p['id']}/transition", {"action": "approve"})
    assert st == 200 and res["status"] == "approved"

    item = designer.call("POST", f"/api/systems/{sid}/items", {"layer": "app", "key": "k", "name": "n", "kind": "document", "content": "x"})[1]
    assert designer.call("POST", f"/api/items/{item['id']}/transition", {"action": "request_review"})[0] == 200
    assert designer.call("POST", f"/api/items/{item['id']}/transition", {"action": "approve"})[0] == 403
    assert approver.call("POST", f"/api/items/{item['id']}/transition", {"action": "approve"})[0] == 200
    assert designer.call("POST", "/api/baselines/1/approve", {})[0] == 403
    audit = a.call("GET", f"/api/systems/{sid}/audit")[1]
    assert {x["actor"] for x in audit if x["action"] in ("param.approve", "item.approve")} == {"apr"}


def test_api_tokens_mcp_a2a(server):
    a = admin(server)
    des = _user(server, a, "des", "designer")
    st, tok, _ = des.call("POST", "/api/auth/tokens", {"name": "CI"})
    assert st == 200 and tok["token"].startswith("rvz_") and tok["prefix"] == tok["token"][:10]
    assert des.call("GET", "/api/auth/tokens")[1] == [{"id": tok["id"], "name": "CI", "prefix": tok["prefix"], "created_at": des.call("GET", "/api/auth/tokens")[1][0]["created_at"], "last_used_at": None}]
    bearer = {"Authorization": f"Bearer {tok['token']}"}

    # REST API: トークンなら Cookie も CSRF も不要
    api = Client(server)
    assert api.call("GET", "/api/systems", headers={"Authorization": "Bearer rvz_invalid"})[0] == 401
    st, s, _ = api.call("POST", "/api/systems", {"name": "T"}, headers=bearer)
    assert st == 200 and api.call("GET", "/api/users", headers=bearer)[0] == 403
    assert des.call("GET", "/api/auth/tokens")[1][0]["last_used_at"] is not None

    # MCP: トークンなしは 401、ありならトークンの持ち主として操作する
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientCapabilities": {}}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create_system", "arguments": {"name": "via-mcp"}, "_meta": meta}}
    mh = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "create_system"}
    st, res, hdrs = api.call("POST", "/mcp", body, headers=mh)
    assert st == 401 and hdrs["WWW-Authenticate"].startswith("Bearer")
    assert api.call("POST", "/mcp", body, headers={**mh, "Cookie": des.cookie})[0] == 401  # Cookie では通さない
    st, res, _ = api.call("POST", "/mcp", body, headers={**mh, **bearer, "X-Reqviz-User": "root"})
    assert st == 200 and res["result"]["structuredContent"]["name"] == "via-mcp"
    body["params"].update(name="api_request", arguments={"method": "GET", "path": "/api/users"})
    st, res, _ = api.call("POST", "/mcp", body, headers={**mh, **bearer, "Mcp-Name": "api_request"})
    assert res["result"]["isError"] is True and "HTTP 403" in res["result"]["content"][0]["text"]  # ロールも引き継ぐ

    # A2A: Agent Card は公開で、認証方式を載せる
    st, card, _ = api.call("GET", "/.well-known/agent-card.json")
    assert st == 200 and card["securitySchemes"]["apiToken"]["httpAuthSecurityScheme"]["scheme"] == "Bearer"
    msg = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": {
        "messageId": "m1", "role": "ROLE_USER", "parts": [{"data": {"tool": "create_system", "arguments": {"name": "via-a2a"}}}]}}}
    assert api.call("POST", "/a2a", msg, headers={"A2A-Version": "1.0"})[0] == 401
    st, res, _ = api.call("POST", "/a2a", msg, headers={"A2A-Version": "1.0", **bearer})
    assert st == 200 and res["result"]["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
    creators = [x["actor"] for x in a.call("GET", "/api/audit")[1] if x["action"] == "system.create"]
    assert creators == ["des", "des", "des"]

    # 他人のトークンは消せない。失効・アカウント無効化で使えなくなる
    assert a.call("DELETE", f"/api/auth/tokens/{tok['id']}")[0] == 404
    uid = next(u["id"] for u in a.call("GET", "/api/users")[1] if u["login"] == "des")
    a.call("PUT", f"/api/users/{uid}", {"disabled": True})
    assert api.call("GET", "/api/systems", headers=bearer)[0] == 401
    a.call("PUT", f"/api/users/{uid}", {"disabled": False})
    des2 = Client(server)
    des2.login("des", "des-password")
    assert des2.call("DELETE", f"/api/auth/tokens/{tok['id']}")[1] == {"ok": True}
    assert api.call("GET", "/api/systems", headers=bearer)[0] == 401


def test_bootstrap_and_disabled_mode(tmp_path):
    cfg = Config(host="127.0.0.1", port=0, db_path=tmp_path / "b.db", templates_dir=ROOT / "templates", llm=LLMConfig())
    srv = make_server(cfg, run_ai_in_thread=False)
    login, password = srv.app.bootstrap_admin  # パスワード未指定なら自動生成し、初回に変更させる
    srv.server_close()
    assert login == "admin" and len(password) >= 12
    srv = make_server(cfg, run_ai_in_thread=False)
    assert srv.app.bootstrap_admin is None  # 2 回目以降は作らない
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = Client(f"http://127.0.0.1:{srv.server_address[1]}")
    assert c.login(login, password)[1]["user"]["must_change_password"] is True
    srv.shutdown()
    srv.server_close()

    off = make_server(Config(host="127.0.0.1", port=0, db_path=tmp_path / "off.db", templates_dir=ROOT / "templates",
                             llm=LLMConfig(), auth_enabled=False), run_ai_in_thread=False)
    threading.Thread(target=off.serve_forever, daemon=True).start()
    c = Client(f"http://127.0.0.1:{off.server_address[1]}")
    assert c.call("GET", "/api/auth/me")[1] == {"auth_enabled": False, "user": None, "csrf_token": None, "roles": auth.ROLE_LABELS}
    assert c.call("GET", "/api/systems")[0] == 200 and c.call("POST", "/api/auth/login", {"login": "a", "password": "b"})[0] == 400
    off.shutdown()
    off.server_close()
