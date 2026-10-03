"""起動: uv run python -m reqviz"""

from __future__ import annotations

import logging

from . import config
from .server import make_server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load()
    server = make_server(cfg)
    host, port = server.server_address[:2]
    print(f"reqviz を起動しました: http://{host}:{port}/  （DB: {cfg.db_path}）")
    print("LLM:", f"{cfg.llm.base_url} / {cfg.llm.model}" if cfg.llm.enabled else "未設定（画面の設定から指定できます）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("停止します")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
