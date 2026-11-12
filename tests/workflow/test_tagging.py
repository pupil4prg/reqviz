import pytest

from reqviz.errors import Invalid
from reqviz.io import export
from reqviz.repo import history
from reqviz.repo import model as repo
from reqviz.workflow import editing, items, snapshot, tagging, templates, versions


def param(ctx, sid, ref):
    return repo.params_by_ref(ctx, sid)[ref]


@pytest.fixture
def tagged(ctx, web3):
    """Web三層 + 構成項目 2 件に、いくつかタグを付けたもの。"""
    editing.update_param(ctx, param(ctx, web3, "db.max_connections")["id"], {
        "tags": ["開発中", "DB"], "aliases": [{"label": "実装名", "value": "max_connections"}]})
    editing.update_param(ctx, param(ctx, web3, "db.reserved_connections")["id"], {"tags": ["確定", "DB"]})
    editing.update_param(ctx, param(ctx, web3, "os.max_open_files")["id"], {"tags": ["開発中"], "rationale": "ulimit -n の設定"})
    items.create_item(ctx, web3, {"layer": "app", "key": "session_store", "name": "セッション管理方式", "kind": "choice",
                                  "options": [{"key": "redis", "label": "Redis に外部化"}, "sticky"], "selected": "redis", "tags": ["開発中"]})
    items.create_item(ctx, web3, {"layer": "os", "key": "sysctl", "name": "カーネルパラメータ", "kind": "document",
                                  "content_type": "json", "content": {"net.core.somaxconn": 4096}, "tags": ["OS"]})
    return web3


def test_normalize(ctx, web3):
    p = param(ctx, web3, "biz.peak_tps")
    assert p["tags"] == []
    assert editing.update_param(ctx, p["id"], {"tags": [" 開発中 ", "DB", "db", "", "開発中"]})["tags"] == ["開発中", "DB"]
    assert editing.update_param(ctx, p["id"], {"tags": "要確認、 性能 ,要確認"})["tags"] == ["要確認", "性能"]
    assert editing.update_param(ctx, p["id"], {"value": 250})["tags"] == ["要確認", "性能"]
    for bad in (1, [1], ["a,b"], ["x" * 33], ["a\nb"], [f"t{i}" for i in range(21)]):
        with pytest.raises(Invalid):
            editing.update_param(ctx, p["id"], {"tags": bad})
    assert editing.update_param(ctx, p["id"], {"tags": []})["tags"] == []


def test_tag_change_keeps_approval(ctx, web3):
    p = param(ctx, web3, "biz.peak_tps")
    ctx.conn.execute("UPDATE params SET status = 'approved' WHERE id = ?", (p["id"],))
    assert editing.update_param(ctx, p["id"], {"tags": ["確定"]})["status"] == "approved"
    rev = history.list_revisions(ctx, web3, "param", "biz.peak_tps")[0]
    assert rev["changes"] == [{"field": "tags", "before": [], "after": ["確定"]}]


def test_tag_counts(ctx, tagged):
    assert tagging.tag_counts(ctx, tagged) == [
        {"tag": "DB", "params": 2, "items": 0}, {"tag": "OS", "params": 0, "items": 1},
        {"tag": "確定", "params": 1, "items": 0}, {"tag": "開発中", "params": 2, "items": 1},
    ]


def refs(result):
    return [r["ref"] for r in result["results"]]


def test_search_by_tag(ctx, tagged):
    assert refs(tagging.search(ctx, tagged, tags="開発中")) == ["db.max_connections", "os.max_open_files", "app.session_store"]
    assert refs(tagging.search(ctx, tagged, tags=["開発中", "db"])) == ["db.max_connections"]  # 既定は AND、大文字小文字は区別しない
    assert refs(tagging.search(ctx, tagged, tags="確定,OS", tag_mode="any")) == ["db.reserved_connections", "os.sysctl"]
    assert refs(tagging.search(ctx, tagged, tags="開発中", kind="param")) == ["db.max_connections", "os.max_open_files"]
    assert refs(tagging.search(ctx, tagged, tags="開発中", kind="choice")) == ["app.session_store"]
    assert refs(tagging.search(ctx, tagged, tags="開発中", layer="os")) == ["os.max_open_files"]
    assert tagging.search(ctx, tagged, tags="なし") == {"total": 0, "results": []}


def test_search_by_text(ctx, tagged):
    r = tagging.search(ctx, tagged, q="max_connections")
    assert refs(r) == ["db.max_connections"] and r["results"][0]["matched"] == ["ref", "alias"]
    assert r["results"][0]["summary"] == "200 本" and r["results"][0]["type"] == "param"
    assert refs(tagging.search(ctx, tagged, q="ULIMIT")) == ["os.max_open_files"]            # 根拠
    assert refs(tagging.search(ctx, tagged, q="外部化")) == ["app.session_store"]             # 選択肢の名前
    assert refs(tagging.search(ctx, tagged, q="somaxconn")) == ["os.sysctl"]                  # 不定形データの本文
    assert refs(tagging.search(ctx, tagged, q="DB 開発中")) == ["db.max_connections"]         # 語は AND。タグも対象
    assert refs(tagging.search(ctx, tagged, q="コネクション", tags="確定")) == ["db.reserved_connections"]
    assert tagging.search(ctx, tagged)["total"] == 16 and len(tagging.search(ctx, tagged, limit=3)["results"]) == 3

    assert refs(tagging.search(ctx, tagged, status="stale", layer="db")) == ["db.pool_ratio", "db.pool_per_server"]
    ctx.conn.execute("UPDATE params SET status = 'approved' WHERE key = 'max_connections'")
    assert refs(tagging.search(ctx, tagged, status="approved")) == ["db.max_connections"]
    for kw in ({"tag_mode": "x"}, {"kind": "rule"}, {"status": "done"}):
        with pytest.raises(Invalid):
            tagging.search(ctx, tagged, **kw)


def test_apply_and_rename(ctx, tagged):
    res = tagging.apply_tags(ctx, tagged, ["db.max_connections", "app.session_store", "db.reserved_connections", "db.max_connections"],
                             add=["確定"], remove=["開発中"], comment="レビュー完了")
    assert res == {"updated": ["db.max_connections", "app.session_store"], "unchanged": ["db.reserved_connections"]}
    assert param(ctx, tagged, "db.max_connections")["tags"] == ["DB", "確定"]
    assert repo.items_by_ref(ctx, tagged)["app.session_store"]["tags"] == ["確定"]
    assert history.list_revisions(ctx, tagged, "item", "app.session_store")[0]["comment"] == "レビュー完了"
    assert history.list_audit(ctx, tagged)[0]["action"] == "tags.apply"

    with pytest.raises(Invalid, match="存在しない"):
        tagging.apply_tags(ctx, tagged, ["db.nope"], add=["x"])
    with pytest.raises(Invalid, match="add"):
        tagging.apply_tags(ctx, tagged, ["db.max_connections"])

    res = tagging.rename_tag(ctx, tagged, "db", "データベース")
    assert sorted(res["updated"]) == ["db.max_connections", "db.reserved_connections"]
    assert param(ctx, tagged, "db.max_connections")["tags"] == ["確定", "データベース"]
    tagging.rename_tag(ctx, tagged, "開発中", "確定")  # 既にあるタグへの変更は統合
    assert {c["tag"]: c["params"] + c["items"] for c in tagging.tag_counts(ctx, tagged)} == {"OS": 1, "確定": 4, "データベース": 2}
    with pytest.raises(Invalid, match="使われていません"):
        tagging.rename_tag(ctx, tagged, "開発中", "x")
    with pytest.raises(Invalid):
        tagging.rename_tag(ctx, tagged, "a,b", "x")


def test_tags_in_snapshot_export_history(ctx, tagged):
    payload = snapshot.export_payload(ctx, tagged)
    assert next(p for p in payload["params"] if p["key"] == "max_connections")["tags"] == ["開発中", "DB"]
    clone = templates.clone_system(ctx, tagged, "複製")
    assert param(ctx, clone["id"], "db.max_connections")["tags"] == ["開発中", "DB"]
    assert "開発中; DB" in export.to_csv(ctx, tagged).decode("utf-8-sig")

    before = versions.timeline(ctx, tagged, "param", "db.max_connections")[0]
    editing.update_param(ctx, param(ctx, tagged, "db.max_connections")["id"], {"tags": []})
    assert versions.state_at(ctx, tagged, f"rev:{before['id']}")["params"]["db.max_connections"]["tags"] == ["開発中", "DB"]
    assert versions.restore_revision(ctx, before["id"])["result"]["tags"] == ["開発中", "DB"]
