import io

import pytest

from reqviz import auth, cli, db
from reqviz.context import Ctx


@pytest.fixture(autouse=True)
def fast_hash(monkeypatch):
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)
    auth.reset_failures()


@pytest.fixture
def env(tmp_path):
    path = tmp_path / "cli.db"
    conn = db.connect(path)
    db.migrate(conn)
    ctx = Ctx(conn, "setup")
    auth.create_user(ctx, {"login": "admin", "role": "admin", "password": "forgotten-pass", "must_change_password": False})
    auth.create_user(ctx, {"login": "sato", "role": "designer", "password": "initial-pass", "display_name": "佐藤"})
    conn.close()
    return {"REQVIZ_DB_PATH": str(path)}


def run(env, *argv, stdin="", answers=()):
    out, it = io.StringIO(), iter(answers)
    code = cli.main(list(argv), env, io.StringIO(stdin), out, lambda _prompt: next(it))
    return code, out.getvalue()


def conn_of(env):
    return db.connect(env["REQVIZ_DB_PATH"])


def test_users(env):
    code, out = run(env, "users")
    assert code == 0
    assert out.splitlines() == ["admin\t管理者\t有効\tadmin", "sato\t設計者\t初期パスワード\t佐藤"]


def test_reset_password_interactive(env, capsys):
    c = conn_of(env)
    ctx = Ctx(c, "admin")
    token, principal = auth.login(ctx, "admin", "forgotten-pass")
    api_token = auth.create_token(ctx, principal, "ci")["token"]

    code, out = run(env, "reset-password", "ADMIN", answers=["new-initial-pass", "new-initial-pass"])
    assert code == 0 and "admin のパスワードを再設定しました" in out and "new-initial-pass" not in out

    with pytest.raises(auth.Unauthorized):
        auth.login(ctx, "admin", "forgotten-pass")
    _, p = auth.login(ctx, "admin", "new-initial-pass")
    assert p.must_change_password is True and p.role == "admin"
    # ログイン中だったセッションは切れ、API トークンは残る
    assert auth.authenticate(c, {"Cookie": f"{auth.COOKIE_NAME}={token}"}) is None
    assert auth.authenticate(c, {"Authorization": f"Bearer {api_token}"}).login == "admin"
    row = c.execute("SELECT actor, detail_json FROM audit_log WHERE action = 'user.update' ORDER BY id DESC").fetchone()
    assert row["actor"].startswith("cli") and "password" in row["detail_json"]
    c.close()

    assert run(env, "reset-password", "admin", answers=["one-password", "other-password"])[0] == 1
    assert "一致しません" in capsys.readouterr().err
    assert run(env, "reset-password", "admin", answers=["short", "short"])[0] == 1
    assert "8 文字以上" in capsys.readouterr().err


def test_reset_password_generate_stdin_and_revoke(env, capsys):
    code, out = run(env, "reset-password", "sato", "--generate", "--revoke-tokens")
    password = next(l for l in out.splitlines() if l.startswith("初期パスワード: ")).split(": ")[1]
    assert code == 0 and len(password) >= 12 and "API トークンをすべて失効" in out
    c = conn_of(env)
    ctx = Ctx(c, "x")
    _, p = auth.login(ctx, "sato", password)
    tok = auth.create_token(ctx, p, "t")["token"]

    code, out = run(env, "reset-password", "sato", "--password-stdin", "--revoke-tokens", stdin="piped-password-1\n")
    assert code == 0 and "piped-password-1" not in out
    assert auth.login(ctx, "sato", "piped-password-1")[1].login == "sato"
    assert auth.authenticate(c, {"Authorization": f"Bearer {tok}"}) is None

    # 無効なアカウントは、再設定してもログインできないことを知らせる
    auth.update_user(ctx, p.user_id, {"disabled": True})
    code, out = run(env, "reset-password", "sato", "--generate")
    assert code == 0 and "無効になっています" in out
    c.close()

    code, _ = run(env, "reset-password", "nobody", "--generate")
    assert code == 1 and "登録済み: admin, sato" in capsys.readouterr().err
    with pytest.raises(SystemExit):  # 併用できない指定
        run(env, "reset-password", "sato", "--generate", "--password-stdin")


def test_missing_db(tmp_path, capsys):
    code, _ = run({"REQVIZ_DB_PATH": str(tmp_path / "none.db")}, "users")
    assert code == 1 and "DB が見つかりません" in capsys.readouterr().err
    assert not (tmp_path / "none.db").exists()
