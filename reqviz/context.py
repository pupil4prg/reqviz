"""リクエスト単位の実行コンテキスト。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass
class Ctx:
    conn: sqlite3.Connection
    actor: str
