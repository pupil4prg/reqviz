"""起動: uv run python -m reqviz（コマンドの一覧は --help）"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
