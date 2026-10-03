"""設定（環境変数から読み込む）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

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


def load(env: dict[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    return Config(
        host=e.get("REQVIZ_HOST", "127.0.0.1"),
        port=int(e.get("REQVIZ_PORT", "8765")),
        db_path=Path(e.get("REQVIZ_DB_PATH", str(ROOT / "data" / "reqviz.db"))),
        default_user=e.get("REQVIZ_USER", "designer"),
        templates_dir=Path(e.get("REQVIZ_TEMPLATES_DIR", str(ROOT / "templates"))),
        llm=LLMConfig(
            base_url=e.get("REQVIZ_LLM_BASE_URL", "").rstrip("/"),
            model=e.get("REQVIZ_LLM_MODEL", ""),
            api_key=e.get("REQVIZ_LLM_API_KEY", ""),
            timeout=float(e.get("REQVIZ_LLM_TIMEOUT", "60")),
        ),
    )
