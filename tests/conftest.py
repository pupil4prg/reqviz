import json
from pathlib import Path

import pytest

from reqviz import db
from reqviz.context import Ctx

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "web-3tier.json"


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    yield c
    c.close()


@pytest.fixture
def ctx(conn):
    return Ctx(conn=conn, actor="tester")


@pytest.fixture
def web3_payload():
    return json.loads(TEMPLATE.read_text(encoding="utf-8"))


@pytest.fixture
def web3(ctx, web3_payload):
    """Web三層テンプレートから作成したシステム（ID を返す）。"""
    from reqviz.workflow import templates

    t = templates.import_template(ctx, web3_payload)
    s = templates.create_system_from_template(ctx, t["id"], "テスト系")
    return s["id"]
