import time

import pytest

from reqviz.errors import Invalid, NotFound
from reqviz.repo import history
from reqviz.repo import model as repo
from reqviz.workflow import analysis, editing, items, snapshot, templates, versions


def last_rev(ctx, sid):
    return ctx.conn.execute("SELECT MAX(id) FROM revisions WHERE system_id = ?", (sid,)).fetchone()[0]


def current(ctx, sid):
    s = versions.state_at(ctx, sid)
    return {k: s[k] for k in ("params", "items", "rules")}


def test_creation_records_entity_revisions(ctx, web3):
    # テンプレートからの作成でも、エンティティごとの最初の版が残る
    tl = versions.timeline(ctx, web3, "param", "biz.peak_tps")
    assert [(r["version"], r["op"]) for r in tl] == [(1, "create")]
    assert len(history.list_revisions(ctx, web3, "rule")) == 10


def test_timeline_follows_renames(ctx, web3):
    p = repo.params_by_ref(ctx, web3)["biz.peak_tps"]
    editing.update_param(ctx, p["id"], {"value": 250}, comment="見直し")
    editing.update_param(ctx, p["id"], {"key": "peak_rps"})
    editing.update_param(ctx, p["id"], {"value": 300})
    tl = versions.timeline(ctx, web3, "param", "biz.peak_rps")
    assert [r["version"] for r in tl] == [4, 3, 2, 1]
    assert [r["entity_key"] for r in tl] == ["biz.peak_rps", "biz.peak_rps", "biz.peak_tps", "biz.peak_tps"]
    assert tl[2]["comment"] == "見直し"
    with pytest.raises(Invalid):
        versions.timeline(ctx, web3, "layer", "biz")


def test_state_at_and_diff(ctx, web3):
    base = current(ctx, web3)
    r0 = last_rev(ctx, web3)

    by_ref = repo.params_by_ref(ctx, web3)
    editing.update_param(ctx, by_ref["biz.peak_tps"]["id"], {"value": 400})
    r1 = last_rev(ctx, web3)
    c = items.create_item(ctx, web3, {"layer": "app", "key": "store", "name": "方式", "kind": "choice", "options": ["a", "b"], "selected": "a"})
    items.update_item(ctx, c["id"], {"selected": "b"})
    rule = next(r for r in repo.list_rules(ctx, web3) if r["key"] == "C-002")
    editing.delete_rule(ctx, rule["id"])
    editing.update_param(ctx, by_ref["app.server_count"]["id"], {"key": "ap_count"})  # ルールの式も追従して書き換わる
    editing.update_layer(ctx, by_ref["biz.peak_tps"]["layer_id"], {"key": "business"})
    analysis.recalc(ctx, web3, "all")
    analysis.apply_proposals(ctx, web3)

    # 現在の状態から逆にたどって、過去の状態が正確に再現できる
    assert versions.state_at(ctx, web3, f"rev:{r0}")["params"] == base["params"]
    assert versions.state_at(ctx, web3, f"rev:{r0}")["rules"] == base["rules"]
    assert versions.state_at(ctx, web3, f"rev:{r0}")["items"] == {}
    s1 = versions.state_at(ctx, web3, f"rev:{r1}")
    assert s1["params"]["biz.peak_tps"]["value"] == 400 and "status" not in s1["params"]["biz.peak_tps"]

    d = versions.diff(ctx, web3, f"rev:{r0}")
    changes = {(x["key"], x["change"]) for x in d["params"]}
    assert ("biz.peak_tps", "removed") in changes and ("business.peak_tps", "added") in changes
    assert ("app.server_count", "removed") in changes and ("app.ap_count", "added") in changes
    assert d["items"] == [{"key": "app.store", "change": "added", "after": versions.state_at(ctx, web3)["items"]["app.store"]}]
    assert {"key": "C-002", "change": "removed"}.items() <= next(x for x in d["rules"] if x["key"] == "C-002").items()
    f2 = next(x for x in d["rules"] if x["key"] == "F-002")
    assert f2["change"] == "modified" and f2["fields"][0]["field"] == "expression"
    assert versions.diff(ctx, web3, "now", None) == {"from_revision": None, "to_revision": None, "params": [], "items": [], "rules": []}


def test_state_at_by_time(ctx, web3):
    p = repo.params_by_ref(ctx, web3)["biz.peak_tps"]
    before = ctx.conn.execute("SELECT MAX(at) FROM revisions").fetchone()[0]
    time.sleep(1.1)
    editing.update_param(ctx, p["id"], {"value": 999})
    assert versions.state_at(ctx, web3, before)["params"]["biz.peak_tps"]["value"] == 200
    assert versions.state_at(ctx, web3, before.replace("+00:00", "Z"))["params"]["biz.peak_tps"]["value"] == 200
    assert versions.state_at(ctx, web3, "2000-01-01T00:00:00+09:00")["params"] == {}
    assert versions.state_at(ctx, web3, "2999-01-01")["params"]["biz.peak_tps"]["value"] == 999
    with pytest.raises(Invalid, match="ISO 8601"):
        versions.state_at(ctx, web3, "yesterday")


def test_restore_revision_param(ctx, web3):
    analysis.recalc(ctx, web3, "all")
    analysis.apply_proposals(ctx, web3)
    p = repo.params_by_ref(ctx, web3)["biz.peak_tps"]
    v1 = versions.timeline(ctx, web3, "param", "biz.peak_tps")[0]
    editing.update_param(ctx, p["id"], {"value": 500, "rationale": "仮置き"})
    analysis.recalc(ctx, web3, "stale")
    analysis.apply_proposals(ctx, web3)

    res = versions.restore_revision(ctx, v1["id"])
    assert res["result"]["value"] == 200 and res["result"]["rationale"] == v1["after"]["rationale"]
    assert res["result"]["status"] == "draft"
    assert repo.params_by_ref(ctx, web3)["app.concurrent_requests"]["flag_stale"]  # 下流は要再計算
    tl = versions.timeline(ctx, web3, "param", "biz.peak_tps")
    assert f"#{v1['id']}" in tl[0]["comment"]
    assert history.list_audit(ctx, web3, "biz.peak_tps")[0]["action"] == "revision.restore"

    # 削除のリビジョンを指定すると、削除前の内容で作り直す
    target = repo.params_by_ref(ctx, web3)["nfr.target_response_sec"]
    rule = next(r for r in repo.list_rules(ctx, web3) if r["key"] == "C-004")
    editing.delete_rule(ctx, rule["id"])
    for rel in repo.list_relations(ctx, web3):
        editing.delete_relation(ctx, rel["id"])
    editing.delete_param(ctx, target["id"])
    del_rev = versions.timeline(ctx, web3, "param", "nfr.target_response_sec")[0]
    assert del_rev["op"] == "delete"
    assert versions.restore_revision(ctx, del_rev["id"], "誤削除")["result"]["value"] == 2
    rule_del = versions.timeline(ctx, web3, "rule", "C-004")[0]
    assert versions.restore_revision(ctx, rule_del["id"])["result"]["expression"] == rule["expression"]


def test_restore_revision_item_and_errors(ctx, web3):
    c = items.create_item(ctx, web3, {"layer": "app", "key": "store", "name": "方式", "kind": "choice", "options": ["a", "b"], "selected": "a"})
    items.update_item(ctx, c["id"], {"options": ["a", "b", "c"], "selected": "c"})
    d = items.create_item(ctx, web3, {"layer": "db", "key": "memo", "name": "メモ", "kind": "document", "content_type": "json", "content": {"k": 1}})
    items.update_item(ctx, d["id"], {"content": {"k": 2}})
    items.delete_item(ctx, d["id"])

    first = versions.timeline(ctx, web3, "item", "app.store")[-1]
    res = versions.restore_revision(ctx, first["id"])["result"]
    assert res["selected"] == "a" and [o["key"] for o in res["options"]] == ["a", "b"]
    memo_v2 = versions.timeline(ctx, web3, "item", "db.memo")[1]
    assert versions.restore_revision(ctx, memo_v2["id"])["result"]["content"] == {"k": 2}

    with pytest.raises(NotFound):
        versions.restore_revision(ctx, 999999)
    layer_rev = history.list_revisions(ctx, web3, "system")[0]
    with pytest.raises(Invalid, match="個別に復元できません"):
        versions.restore_revision(ctx, layer_rev["id"])
    # 参照名の変更に追従しただけの部分的な記録からは復元できない
    p = repo.params_by_ref(ctx, web3)["app.server_count"]
    editing.update_param(ctx, p["id"], {"key": "ap_count"})
    partial = next(r for r in history.list_revisions(ctx, web3, "rule") if r["comment"] == "参照名の変更に追従")
    with pytest.raises(Invalid, match="一部のフィールド"):
        versions.restore_revision(ctx, partial["id"])


def test_import_and_restore_keep_history_consistent(ctx, web3):
    before = current(ctx, web3)
    r0 = last_rev(ctx, web3)
    payload = snapshot.export_payload(ctx, web3)
    payload["params"] = [p for p in payload["params"] if p["key"] != "max_open_files"]
    payload["rules"] = [r for r in payload["rules"] if r["key"] != "C-003"]
    payload["params"][0]["rationale"] = "取り込みで変更"
    snapshot.replace_with_payload(ctx, web3, payload, keep_status=False, op="update", comment="JSON 取り込み")
    assert versions.timeline(ctx, web3, "param", "os.max_open_files")[0]["op"] == "delete"
    assert versions.state_at(ctx, web3, f"rev:{r0}") == {"revision": r0, **before}
    templates.clone_system(ctx, web3, "copy")
