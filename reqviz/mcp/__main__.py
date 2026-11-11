"""stdio トランスポートの MCP サーバー。

    uv run --no-dev python -m reqviz.mcp                 # DB を直接開く（サーバーの起動は不要）
    uv run --no-dev python -m reqviz.mcp --url http://127.0.0.1:8765   # 起動中のサーバーの REST API を使う

標準入力から 1 行 1 メッセージの JSON-RPC を読み、標準出力に応答を書く。ログは標準エラーに出す。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, TextIO

from .. import __version__, config
from ..agent.tools import ApiClient, HttpClient, InProcessClient
from .protocol import PARSE_ERROR, McpServer, error_response


def serve(server: McpServer, stdin: TextIO, stdout: TextIO, actor: str | None = None) -> None:
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg: Any = json.loads(line)
        except ValueError:
            res: dict[str, Any] | None = error_response(None, PARSE_ERROR, "Parse error")
        else:
            _, res = server.handle(msg, None, actor)
        if res is not None:
            stdout.write(json.dumps(res, ensure_ascii=False) + "\n")
            stdout.flush()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m reqviz.mcp", description="reqviz の MCP サーバー（stdio）")
    parser.add_argument("--url", default=os.environ.get("REQVIZ_API_URL", ""),
                        help="起動中の reqviz サーバーの URL。省略すると DB（REQVIZ_DB_PATH）を直接開く")
    parser.add_argument("--user", default=os.environ.get("REQVIZ_USER", ""), help="履歴・監査ログに残す操作者名")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    client: ApiClient
    if args.url:
        client = HttpClient(args.url)
    else:
        from ..server import App  # 遅延 import（--url のときは DB を開かない）

        client = InProcessClient(App(config.load()))
    for stream in (sys.stdin, sys.stdout):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    serve(McpServer(client, __version__, default_actor="mcp"), sys.stdin, sys.stdout, args.user or None)


if __name__ == "__main__":
    main()
