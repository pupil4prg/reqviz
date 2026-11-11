"""設定（環境変数から読み込む）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class LLMConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""  # 環境変数のみ。DB や画面には出さない
    timeout: float = 60.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8765
    db_path: Path = ROOT / "data" / "reqviz.db"
    default_user: str = "designer"
    templates_dir: Path = ROOT / "templates"
    llm: LLMConfig = field(default_factory=LLMConfig)
    public_url: str = ""  # Agent Card に載せる外向きの URL。空ならリクエストの Host から決める
    allowed_origins: tuple[str, ...] = ()  # /mcp・/a2a で追加で許可する Origin
    agent_endpoints: bool = True  # /mcp・/a2a・Agent Card を公開するか
    auth_enabled: bool = True  # ログイン認証を行うか
    admin_user: str = "admin"  # アカウントが 1 件もないときに作る最初の管理者
    admin_password: str = ""  # 空なら自動生成して起動時に表示する
    session_hours: float = 12.0
    cookie_secure: bool = False  # HTTPS で公開する場合に on（Cookie に Secure を付ける）


def _flag(e: Any, name: str, default: bool) -> bool:
    v = e.get(name)
    if v is None or v == "":
        return default
    return v.lower() not in ("off", "0", "false", "no")


def load(env: dict[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    return Config(
        host=e.get("REQVIZ_HOST", "127.0.0.1"),
        port=int(e.get("REQVIZ_PORT", "8765")),
        db_path=Path(e.get("REQVIZ_DB_PATH", str(ROOT / "data" / "reqviz.db"))),
        default_user=e.get("REQVIZ_USER", "designer"),
        templates_dir=Path(e.get("REQVIZ_TEMPLATES_DIR", str(ROOT / "templates"))),
        public_url=e.get("REQVIZ_PUBLIC_URL", "").rstrip("/"),
        allowed_origins=tuple(o.strip().rstrip("/") for o in e.get("REQVIZ_ALLOWED_ORIGINS", "").split(",") if o.strip()),
        agent_endpoints=_flag(e, "REQVIZ_AGENT_ENDPOINTS", True),
        auth_enabled=_flag(e, "REQVIZ_AUTH", True),
        admin_user=e.get("REQVIZ_ADMIN_USER", "admin"),
        admin_password=e.get("REQVIZ_ADMIN_PASSWORD", ""),
        session_hours=float(e.get("REQVIZ_SESSION_HOURS", "12")),
        cookie_secure=_flag(e, "REQVIZ_COOKIE_SECURE", False),
        llm=LLMConfig(
            base_url=e.get("REQVIZ_LLM_BASE_URL", "").rstrip("/"),
            model=e.get("REQVIZ_LLM_MODEL", ""),
            api_key=e.get("REQVIZ_LLM_API_KEY", ""),
            timeout=float(e.get("REQVIZ_LLM_TIMEOUT", "60")),
        ),
    )
