import pytest

from reqviz.errors import Invalid
from reqviz.repo import history
from reqviz.repo import model as repo
from reqviz.workflow import analysis, baseline, editing, items, param_state, snapshot, templates


def layer(ctx, sid, key):
    return next(l for l in repo.list_layers(ctx, sid) if l["key"] == key)


def choice(ctx, sid, **kw):
    data = {
        "layer": "app", "key": "session_store", "name": "セッション管理方式", "kind": "choice",
        "options": [
            {"key": "sticky", "label": "スティッキーセッション", "pros": ["構成が単純"], "cons": ["障害時にセッションが消える"]},
            {"key": "redis", "label": "Redis に外部化", "description": "AP をステートレスにする", "attributes": {"cost": "中"}},
            "db",
        ],
        "selected": "sticky", "rationale": "初期は単純な構成にする",
    }
    data.update(kw)
    return items.create_item(ctx, sid, data)


def test_choice_crud_and_normalization(ctx, web3):
    c = choice(ctx, web3)
    assert c["ref"] == "app.session_store" and c["status"] == "draft"
    assert [o["key"] for o in c["options"]] == ["sticky", "redis", "db"]
    assert c["options"][2] == {"key": "db", "label": "db", "description": "", "pros": [], "cons": [], "attributes": {}}
    assert c["options"][1]["attributes"] == {"cost": "中"}

    c2 = items.update_item(ctx, c["id"], {"selected": "redis", "row_version": c["row_version"]}, comment="可用性のため")
    assert c2["selected"] == "redis"
    revs = history.list_revisions(ctx, web3, "item", "app.session_store")
    assert [r["op"] for r in revs] == ["update", "create"] and revs[0]["comment"] == "可用性のため"
    assert {"field": "selected", "before": "sticky", "after": "redis"} in revs[0]["changes"]

    items.delete_item(ctx, c["id"])
    assert repo.list_items(ctx, web3) == []


def test_choice_validation(ctx, web3):
    with pytest.raises(Invalid, match="1 つ以上"):
        choice(ctx, web3, options=[])
    with pytest.raises(Invalid, match="重複"):
        choice(ctx, web3, options=["a", "a"])
    with pytest.raises(Invalid, match="いずれか"):
        choice(ctx, web3, selected="nope")
    with pytest.raises(Invalid, match="種別"):
        choice(ctx, web3, kind="enum")
    with pytest.raises(Invalid, match="レイヤー"):
        choice(ctx, web3, layer="zzz")
    c = choice(ctx, web3)
    with pytest.raises(Invalid, match="いずれか"):  # 採用中の選択肢を消す変更は不可
        items.update_item(ctx, c["id"], {"options": ["redis", "db"]})
    with pytest.raises(Invalid, match="種別は変更できません"):
        items.update_item(ctx, c["id"], {"kind": "document"})


def test_ref_namespace_shared_with_params(ctx, web3):
    with pytest.raises(Invalid, match="パラメータが既にあります"):
        choice(ctx, web3, key="server_count")
    choice(ctx, web3)
    with pytest.raises(Invalid, match="構成項目が既にあります"):
        editing.create_param(ctx, web3, {"layer_id": layer(ctx, web3, "app")["id"], "key": "session_store", "name": "x"})


def test_choice_referenced_in_rules(ctx, web3):
    c = choice(ctx, web3)
    analysis.apply_proposals(ctx, web3, [r["ref"] for r in analysis.recalc(ctx, web3, "all")])
    # 採用した方式を条件にした算出式と制約
    editing.update_rule(ctx, next(r for r in repo.list_rules(ctx, web3) if r["key"] == "F-003")["id"],
                        {"expression": "0.5 if app.session_store == 'sticky' else 0.3"})
    editing.create_rule(ctx, web3, {"key": "C-S", "kind": "constraint", "expression": "app.session_store != 'db' or app.server_count <= 4"})
    analysis.apply_proposals(ctx, web3, None)
    assert repo.params_by_ref(ctx, web3)["db.pool_ratio"]["value"] == 0.5

    # 採用案を変えると下流が要再計算になり、再計算で値が変わる
    items.update_item(ctx, c["id"], {"selected": "redis"})
    by_ref = repo.params_by_ref(ctx, web3)
    assert by_ref["db.pool_ratio"]["flag_stale"] and by_ref["db.pool_per_server"]["flag_stale"]
    res = {r["ref"]: r for r in analysis.recalc(ctx, web3, "stale")}
    assert res["db.pool_ratio"]["new_value"] == 0.3

    # 選択肢項目は代入先にできない / 参照されている間は削除できない / 名前変更は式に追従
    errs = editing.check_rule(ctx, web3, {"key": "F-X", "kind": "formula", "target": "app.session_store", "expression": "'redis'"})
    assert any("パラメータだけ" in e for e in errs)
    with pytest.raises(Invalid, match="参照されている"):
        items.delete_item(ctx, c["id"])
    items.update_item(ctx, c["id"], {"key": "session_mode"})
    assert "app.session_mode" in next(r for r in repo.list_rules(ctx, web3) if r["key"] == "F-003")["expression"]
    editing.update_layer(ctx, layer(ctx, web3, "app")["id"], {"key": "ap"})
    assert "ap.session_mode ==" in next(r for r in repo.list_rules(ctx, web3) if r["key"] == "F-003")["expression"]
    assert analysis.graph(ctx, web3)["rule_errors"] == {}
    extra = editing.create_layer(ctx, web3, {"key": "ops", "name": "運用"})
    items.create_item(ctx, web3, {"layer": "ops", "key": "runbook", "name": "手順", "kind": "document", "content": "x"})
    with pytest.raises(Invalid, match="構成項目が 1 件"):
        editing.delete_layer(ctx, extra["id"])


def test_document_items(ctx, web3):
    d = items.create_item(ctx, web3, {
        "layer": "os", "key": "network_memo", "name": "ネットワーク構成メモ", "kind": "document",
        "content_type": "markdown", "content": "# 構成\n- DMZ と内部の 2 セグメント", "tags": ["network", " "],
    })
    assert d["content"].startswith("# 構成") and d["tags"] == ["network"]
    j = items.create_item(ctx, web3, {
        "layer": "os", "key": "sysctl", "name": "カーネルパラメータ", "kind": "document",
        "content_type": "json", "content": {"net.core.somaxconn": 1024, "nested": [1, {"a": None}]},
    })
    assert j["content"]["nested"] == [1, {"a": None}]
    j2 = items.update_item(ctx, j["id"], {"content": '{"net.core.somaxconn": 4096}'})  # JSON 文字列も受け付ける
    assert j2["content"] == {"net.core.somaxconn": 4096}
    with pytest.raises(Invalid, match="JSON として"):
        items.update_item(ctx, j["id"], {"content": "{bad"})
    with pytest.raises(Invalid, match="文字列"):
        items.update_item(ctx, d["id"], {"content": {"a": 1}})
    with pytest.raises(Invalid, match="大きすぎ"):
        items.update_item(ctx, d["id"], {"content": "x" * 1_000_001})
    with pytest.raises(Invalid, match="選択肢"):
        items.update_item(ctx, d["id"], {"selected": "a"})
    assert len(repo.list_items(ctx, web3, "document")) == 2 and repo.list_items(ctx, web3, "choice") == []


def test_item_workflow(ctx, web3):
    c = choice(ctx, web3, selected=None)
    with pytest.raises(Invalid, match="レビュー依頼できません"):
        items.transition(ctx, c["id"], "request_review")
    items.update_item(ctx, c["id"], {"selected": "redis"})
    with pytest.raises(Invalid, match="できません"):
        items.transition(ctx, c["id"], "approve")
    items.transition(ctx, c["id"], "request_review")
    assert [i["ref"] for i in param_state.approval_queue(ctx, web3)["items"]] == ["app.session_store"]
    with pytest.raises(Invalid, match="コメント"):
        items.transition(ctx, c["id"], "reject")
    items.transition(ctx, c["id"], "reject", "根拠が不足")
    items.transition(ctx, c["id"], "request_review")
    assert items.transition(ctx, c["id"], "approve")["status"] == "approved"

    # 名前だけの変更では承認は維持、内容の変更で下書きに戻る
    assert items.update_item(ctx, c["id"], {"name": "セッション方式"})["status"] == "approved"
    assert items.update_item(ctx, c["id"], {"selected": "sticky"})["status"] == "draft"
    actions = [a["action"] for a in history.list_audit(ctx, web3, "app.session_store")]
    assert actions[0] == "item.revert_to_draft" and "item.approve" in actions and "item.reject" in actions


def _approve_all(ctx, sid):
    analysis.recalc(ctx, sid, "all")
    analysis.apply_proposals(ctx, sid)
    for p in repo.list_params(ctx, sid):
        for action in ("skip_ai", "request_review", "approve"):
            param_state.transition(ctx, p["id"], action)


def test_baseline_includes_items(ctx, web3):
    c = choice(ctx, web3)
    _approve_all(ctx, web3)
    state = baseline.readiness(ctx, web3)
    assert not state["ready"] and any("未承認の構成項目" in b for b in state["blockers"])
    items.transition(ctx, c["id"], "request_review")
    items.transition(ctx, c["id"], "approve")
    b = baseline.create_baseline(ctx, web3, "v1")
    baseline.approve_baseline(ctx, b["id"], "warning は許容")
    payload = baseline.get_baseline(ctx, b["id"])["payload"]
    assert payload["items"][0]["selected"] == "sticky" and payload["items"][0]["status"] == "approved"

    items.update_item(ctx, c["id"], {"selected": "redis"})
    doc = items.create_item(ctx, web3, {"layer": "db", "key": "memo", "name": "メモ", "kind": "document", "content": "x"})
    d = baseline.diff(ctx, web3, "current", "working")
    assert {(x["key"], x["change"]) for x in d["items"]} == {("app.session_store", "modified"), ("db.memo", "added")}

    baseline.restore_baseline(ctx, b["id"])
    restored = repo.items_by_ref(ctx, web3)
    assert list(restored) == ["app.session_store"] and restored["app.session_store"]["selected"] == "sticky"
    assert restored["app.session_store"]["status"] == "approved"
    # 版からの復元も、エンティティ単位の履歴に残る
    assert history.list_revisions(ctx, web3, "item", "db.memo")[0]["op"] == "restore"
    assert history.list_revisions(ctx, web3, "item", "app.session_store")[0]["op"] == "restore"
    assert doc["ref"] == "db.memo"


def test_export_import_template_roundtrip(ctx, web3):
    choice(ctx, web3)
    items.create_item(ctx, web3, {"layer": "db", "key": "memo", "name": "メモ", "kind": "document", "content_type": "json", "content": [1, 2]})
    payload = snapshot.export_payload(ctx, web3)
    assert [i["key"] for i in payload["items"]] == ["session_store", "memo"]

    s2 = editing.create_system(ctx, {"name": "取り込み先"})
    snapshot.replace_with_payload(ctx, s2["id"], payload, keep_status=False, op="update")
    assert snapshot.export_payload(ctx, s2["id"], include_status=False)["items"] == \
        snapshot.export_payload(ctx, web3, include_status=False)["items"]

    t = templates.template_from_system(ctx, web3, "項目つき")
    assert t["summary"]["items"] == 2
    assert "status" not in templates.get_template(ctx, t["id"])["payload"]["items"][0]
    s3 = templates.create_system_from_template(ctx, t["id"], "テンプレから")
    assert len(repo.list_items(ctx, s3["id"])) == 2
    s4 = templates.clone_system(ctx, web3, "複製")
    assert len(repo.list_items(ctx, s4["id"])) == 2

    bad = {**payload, "items": payload["items"] + [{**payload["params"][0], "kind": "document", "content": "x"}]}
    with pytest.raises(Invalid, match="重複"):
        snapshot.replace_with_payload(ctx, s2["id"], bad, keep_status=False, op="update")
