"""コマンドライン。サーバーの起動と、DB を直接操作するアカウント管理。

    python -m reqviz                         サーバーを起動する
    python -m reqviz users                   アカウントの一覧
    python -m reqviz reset-password <ID>     パスワードを再設定する

アカウント管理のコマンドは DB ファイル（REQVIZ_DB_PATH）を直接開くので、サーバーが動いていても止まっていても使える。
DB ファイルを読み書きできる人だけが実行できる、という前提で認証は行わない。
"""

from __future__ import annotations

import argparse
import getpass
import logging
import secrets
import sys
from typing import Any, Callable, TextIO

from . import auth, config, db
from .context import Ctx
from .errors import AppError


def serve(cfg: config.Config) -> int:
    from .server import make_server

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = make_server(cfg)
    host, port = server.server_address[:2]
    print(f"reqviz を起動しました: http://{host}:{port}/  （DB: {cfg.db_path}）")
    if not cfg.auth_enabled:
        print("ログイン認証: 無効（REQVIZ_AUTH=off）")
    elif server.app.bootstrap_admin:  # type: ignore[attr-defined]
        login, password = server.app.bootstrap_admin  # type: ignore[attr-defined]
        print(f"最初の管理者アカウントを作成しました。ID: {login}  初期パスワード: {password}")
        print("  このパスワードは今回だけ表示されます。ログイン後に変更してください。")
    print("LLM:", f"{cfg.llm.base_url} / {cfg.llm.model}" if cfg.llm.enabled else "未設定（画面の設定から指定できます）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("停止します")
    finally:
        server.server_close()
    return 0


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"[:64]
    except Exception:  # ユーザー名を取得できない環境
        return "cli"


def _open(cfg: config.Config) -> Ctx:
    if not cfg.db_path.is_file():
        raise AppError(f"DB が見つかりません: {cfg.db_path}（REQVIZ_DB_PATH を確認してください）")
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    return Ctx(conn, _actor())


def list_users(cfg: config.Config, out: TextIO) -> int:
    ctx = _open(cfg)
    try:
        users = auth.list_users(ctx)
    finally:
        ctx.conn.close()
    if not users:
        print("アカウントはありません。サーバーを起動すると、最初の管理者が作成されます。", file=out)
        return 0
    for u in users:
        state = "無効" if u["disabled"] else "初期パスワード" if u["must_change_password"] else "有効"
        print(f"{u['login']}\t{auth.ROLE_LABELS[u['role']]}\t{state}\t{u['display_name']}", file=out)
    return 0


def _read_password(args: argparse.Namespace, stdin: TextIO, prompt: Callable[[str], str]) -> tuple[str, bool]:
    """(パスワード, 自動生成したか) を返す。パスワードはコマンドライン引数では受け取らない（履歴やプロセス一覧に残るため）。"""
    if args.generate:
        return secrets.token_urlsafe(12), True
    if args.password_stdin:
        return stdin.readline().rstrip("\r\n"), False
    first = prompt("新しい初期パスワード: ")
    if first != prompt("もう一度入力: "):
        raise AppError("入力した 2 つのパスワードが一致しません")
    return first, False


def reset_password(cfg: config.Config, args: argparse.Namespace, stdin: TextIO, out: TextIO, prompt: Callable[[str], str]) -> int:
    ctx = _open(cfg)
    try:
        # 先に ID を確認し、存在しない ID でパスワードを入力させない
        if ctx.conn.execute("SELECT 1 FROM users WHERE login = ? COLLATE NOCASE", (args.login.strip(),)).fetchone() is None:
            known = ", ".join(u["login"] for u in auth.list_users(ctx)) or "なし"
            raise AppError(f"アカウントが見つかりません: {args.login}（登録済み: {known}）")
        password, generated = _read_password(args, stdin, prompt)
        user = auth.reset_password(ctx, args.login, password, revoke_tokens=args.revoke_tokens)
    finally:
        ctx.conn.close()
    print(f"{user['login']} のパスワードを再設定しました。", file=out)
    if generated:
        print(f"初期パスワード: {password}", file=out)
    print("次のログイン時に、本人がパスワードを変更します。ログイン中の端末はログアウトされました。", file=out)
    if args.revoke_tokens:
        print("発行済みの API トークンをすべて失効させました。", file=out)
    if user["disabled"]:
        print("注意: このアカウントは無効になっています。ログインするには、管理者が画面で有効に戻す必要があります。", file=out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m reqviz", description="reqviz — 要件パラメータ可視化ツール")
    sub = parser.add_subparsers(dest="command", metavar="コマンド")
    sub.add_parser("serve", help="サーバーを起動する（コマンド省略時と同じ）")
    sub.add_parser("users", help="アカウントの一覧を表示する")
    rp = sub.add_parser(
        "reset-password", help="アカウントのパスワードを再設定する",
        description="パスワードを再設定する。入力した値は初期パスワードになり、本人は次のログイン時に変更する。",
    )
    rp.add_argument("login", help="対象の ID")
    how = rp.add_mutually_exclusive_group()
    how.add_argument("--generate", action="store_true", help="パスワードを自動生成して表示する")
    how.add_argument("--password-stdin", action="store_true", help="パスワードを標準入力の 1 行目から読む（自動化用）")
    rp.add_argument("--revoke-tokens", action="store_true", help="発行済みの API トークンも失効させる（漏えいが疑われる場合）")
    return parser


def main(
    argv: list[str] | None = None,
    env: dict[str, str] | None = None,
    stdin: TextIO | None = None,
    out: TextIO | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> int:
    args: Any = build_parser().parse_args(argv)
    cfg = config.load(env)
    out = out or sys.stdout
    try:
        if args.command in (None, "serve"):
            return serve(cfg)
        if args.command == "users":
            return list_users(cfg, out)
        return reset_password(cfg, args, stdin or sys.stdin, out, prompt)
    except AppError as e:
        print(f"エラー: {e.message}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\n中止しました", file=sys.stderr)
        return 130
