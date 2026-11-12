import pytest

from reqviz.errors import Invalid
from reqviz.io import export
from reqviz.repo import history
from reqviz.repo import model as repo
from reqviz.workflow import baseline, editing, snapshot, templates, versions

IMPL = {"label": "実装名", "value": "max_connections"}
LOGICAL = {"label": "論理名", "value": "DB最大同時接続数"}


def param(ctx, sid, ref="db.max_connections"):
    return repo.params_by_ref(ctx, sid)[ref]


def test_set_and_normalize(ctx, web3):
    p = param(ctx, web3)
    assert p["aliases"] == []
    p = editing.update_param(ctx, p["id"], {"aliases": [IMPL, " postgresql.max_connections ", IMPL, {"label": " 論理名 ", "value": "DB最大同時接続数"}]})
    assert p["aliases"] == [IMPL, {"label": "", "value": "postgresql.max_connections"}, LOGICAL]
    assert editing.update_param(ctx, p["id"], {"aliases": {"英語名": "Max connections"}})["aliases"] == [{"label": "英語名", "value": "Max connections"}]
    assert editing.update_param(ctx, p["id"], {"unit": "本"})["aliases"] != []  # 他の項目の更新では消えない
    assert editing.update_param(ctx, p["id"], {"aliases": None})["aliases"] == []

    for bad in ("x", [1], [{"label": "a"}], [{"value": " "}], [{"label": "x" * 33, "value": "v"}], [{"value": "v" * 201}],
                [{"value": "a\nb"}], [str(i) for i in range(21)]):
        with pytest.raises(Invalid):
            editing.update_param(ctx, p["id"], {"aliases": bad})

    layer = repo.get_layer(ctx, p["layer_id"])
    created = editing.create_param(ctx, web3, {"layer_id": layer["id"], "key": "wal_level", "name": "WAL レベル", "type": "string", "aliases": ["wal_level"]})
    assert created["aliases"] == [{"label": "", "value": "wal_level"}]


def test_alias_change_keeps_approval_and_is_recorded(ctx, web3):
    p = param(ctx, web3)
    ctx.conn.execute("UPDATE params SET status = 'approved' WHERE id = ?", (p["id"],))
    after = editing.update_param(ctx, p["id"], {"aliases": [IMPL]}, comment="実装名を登録")
    assert after["status"] == "approved"  # 別名は値ではないので、承認は維持する
    rev = history.list_revisions(ctx, web3, "param", "db.max_connections")[0]
    assert rev["changes"] == [{"field": "aliases", "before": [], "after": [IMPL]}] and rev["comment"] == "実装名を登録"


def test_lookup(ctx, web3):
    editing.update_param(ctx, param(ctx, web3)["id"], {"aliases": [IMPL, LOGICAL]})
    editing.update_param(ctx, param(ctx, web3, "os.max_open_files")["id"], {"aliases": ["nofile", {"label": "実装名", "value": "MAX_CONNECTIONS"}]})

    hits = repo.lookup_params(ctx, web3, "Max_Connections")
    assert {h["ref"]: h["matched"] for h in hits} == {
        "db.max_connections": ["key", "alias:実装名"], "os.max_open_files": ["alias:実装名"],
    }
    assert [h["ref"] for h in repo.lookup_params(ctx, web3, "DB最大同時接続数")] == ["db.max_connections"]
    assert repo.lookup_params(ctx, web3, " nofile ")[0]["matched"] == ["alias"]
    assert repo.lookup_params(ctx, web3, "biz.peak_tps")[0]["matched"] == ["ref"]
    assert repo.lookup_params(ctx, web3, "ピーク時リクエスト数")[0]["matched"] == ["name"]
    assert repo.lookup_params(ctx, web3, "max_conn") == []  # 部分一致はしない


def test_aliases_in_snapshot_template_export(ctx, web3):
    editing.update_param(ctx, param(ctx, web3)["id"], {"aliases": [IMPL, LOGICAL]})
    payload = snapshot.export_payload(ctx, web3)
    assert next(p for p in payload["params"] if p["key"] == "max_connections")["aliases"] == [IMPL, LOGICAL]

    clone = templates.clone_system(ctx, web3, "複製")
    assert param(ctx, clone["id"])["aliases"] == [IMPL, LOGICAL]
    t = templates.template_from_system(ctx, web3, "別名つき")
    assert param(ctx, templates.create_system_from_template(ctx, t["id"], "x")["id"])["aliases"] == [IMPL, LOGICAL]

    d = baseline.diff(ctx, clone["id"], "working", "working")
    assert d["params"] == []
    editing.update_param(ctx, param(ctx, clone["id"])["id"], {"aliases": [IMPL]})
    a, b = snapshot.export_payload(ctx, web3), snapshot.export_payload(ctx, clone["id"])
    changed = snapshot.diff_payloads(a, b)["params"]
    assert [(x["key"], [f["field"] for f in x["fields"]]) for x in changed] == [("db.max_connections", ["aliases"])]

    csv_text = export.to_csv(ctx, web3).decode("utf-8-sig")
    assert "別名" in csv_text.splitlines()[0] and "実装名=max_connections; 論理名=DB最大同時接続数" in csv_text
    assert "実装名=max_connections" in export.to_html(ctx, web3).decode()


def test_history_and_restore(ctx, web3):
    p = param(ctx, web3)
    r0 = ctx.conn.execute("SELECT MAX(id) FROM revisions").fetchone()[0]
    editing.update_param(ctx, p["id"], {"aliases": [IMPL]})
    with_alias = versions.timeline(ctx, web3, "param", "db.max_connections")[0]
    editing.update_param(ctx, p["id"], {"aliases": []})

    assert versions.state_at(ctx, web3, f"rev:{with_alias['id']}")["params"]["db.max_connections"]["aliases"] == [IMPL]
    assert versions.state_at(ctx, web3, f"rev:{r0}")["params"]["db.max_connections"]["aliases"] == []
    assert versions.restore_revision(ctx, with_alias["id"])["result"]["aliases"] == [IMPL]

    # 別名の導入前に記録された履歴（aliases を持たない）から復元しても、現在の別名は消さない
    old = versions.timeline(ctx, web3, "param", "db.max_connections")[-1]
    ctx.conn.execute(
        "UPDATE revisions SET after_json = json_remove(after_json, '$.aliases') WHERE id = ?", (old["id"],)
    )
    assert versions.restore_revision(ctx, old["id"])["result"]["aliases"] == [IMPL]
