import pytest

from reqviz.errors import Invalid, NotFound
from reqviz.io import export
from reqviz.repo import history
from reqviz.repo import model as repo
from reqviz.workflow import analysis, baseline, components, editing, items, param_state, snapshot, templates, versions

# Web三層テンプレートのレイヤー（設計工程）とは別に、構成要素で分ける
ASSIGN = {
    "ap": ["app.concurrent_requests", "app.server_count", "app.threads_per_server", "db.pool_ratio", "db.pool_per_server"],
    "pg": ["db.max_connections", "db.reserved_connections"],
    "os": ["os.max_user_processes", "os.max_open_files"],
}


def param(ctx, sid, ref):
    return repo.params_by_ref(ctx, sid)[ref]


@pytest.fixture
def comp(ctx, web3):
    components.create_component(ctx, web3, {"key": "ap", "name": "AP サーバー", "kind": "ミドルウェア"})
    components.create_component(ctx, web3, {"key": "pg", "name": "PostgreSQL", "kind": "ミドルウェア", "description": "DB サーバー"})
    components.create_component(ctx, web3, {"key": "os", "name": "RHEL", "kind": "OS"})
    for key, refs in ASSIGN.items():
        components.assign(ctx, web3, refs, key)
    return web3


def test_component_crud(ctx, web3):
    c = components.create_component(ctx, web3, {"key": "mw", "name": "ミドルウェア", "kind": "MW"})
    assert (c["key"], c["sort_order"], c["param_count"], c["item_count"]) == ("mw", 0, 0, 0)
    assert components.create_component(ctx, web3, {"key": "os", "name": "OS"})["sort_order"] == 1
    for bad in ({"key": "mw", "name": "重複"}, {"key": "1x", "name": "n"}, {"key": "ok"}, {"key": "k", "name": "n", "kind": "x" * 33}):
        with pytest.raises(Invalid):
            components.create_component(ctx, web3, bad)
    c2 = components.update_component(ctx, c["id"], {"key": "middleware", "name": "MW 層", "row_version": c["row_version"]})
    assert c2["key"] == "middleware" and c2["kind"] == "MW"
    with pytest.raises(Invalid, match="重複"):
        components.update_component(ctx, c["id"], {"key": "os"})
    assert [r["op"] for r in history.list_revisions(ctx, web3, "component", "middleware")] == ["update"]
    with pytest.raises(NotFound):
        components.get_component(ctx, 999)


def test_assign_and_param_fields(ctx, comp):
    p = param(ctx, comp, "db.max_connections")
    assert (p["component"], p["component_name"]) == ("pg", "PostgreSQL")
    assert {c["key"]: c["param_count"] for c in components.list_components(ctx, comp)} == {"ap": 5, "pg": 2, "os": 2}

    ctx.conn.execute("UPDATE params SET status = 'approved' WHERE id = ?", (p["id"],))
    moved = editing.update_param(ctx, p["id"], {"component": "os"}, comment="所属の見直し")
    assert moved["component"] == "os" and moved["status"] == "approved"  # 所属の変更では承認を維持
    rev = history.list_revisions(ctx, comp, "param", "db.max_connections")[0]
    assert rev["changes"] == [{"field": "component", "before": "pg", "after": "os"}]
    assert editing.update_param(ctx, p["id"], {"unit": "本"})["component"] == "os"
    assert editing.update_param(ctx, p["id"], {"component": None})["component"] is None
    assert editing.update_param(ctx, p["id"], {"component_id": components.list_components(ctx, comp)[1]["id"]})["component"] == "pg"
    with pytest.raises(Invalid, match="存在しません"):
        editing.update_param(ctx, p["id"], {"component": "nope"})

    i = items.create_item(ctx, comp, {"layer": "os", "key": "sysctl", "name": "sysctl", "kind": "document", "content": "x", "component": "os"})
    assert i["component"] == "os"
    res = components.assign(ctx, comp, ["os.sysctl", "db.max_connections", "biz.peak_tps"], "pg")
    assert res == {"updated": ["os.sysctl", "biz.peak_tps"], "unchanged": ["db.max_connections"]}
    assert components.assign(ctx, comp, ["biz.peak_tps"], None)["updated"] == ["biz.peak_tps"]
    with pytest.raises(Invalid):
        components.assign(ctx, comp, ["x.y"], "pg")
    with pytest.raises(Invalid):
        components.assign(ctx, comp, ["biz.peak_tps"], "nope")


def test_deps_crud(ctx, comp):
    d = components.create_dep(ctx, comp, {"from": "pg", "to": "os", "note": "共有メモリとファイル数に波及"})
    assert (d["from_key"], d["to_name"], d["notify"]) == ("pg", "RHEL", False)
    for bad in ({"from": "pg", "to": "os"}, {"from": "pg", "to": "pg"}, {"from": "pg", "to": "zz"}, {"from": "pg"}):
        with pytest.raises(Invalid):
            components.create_dep(ctx, comp, bad)
    components.create_dep(ctx, comp, {"from": "os", "to": "pg"})  # 逆向きは別の依存として定義できる
    d2 = components.update_dep(ctx, d["id"], {"notify": True})
    assert d2["notify"] is True and d2["note"] == "共有メモリとファイル数に波及"
    assert len(components.list_deps(ctx, comp)) == 2
    components.delete_dep(ctx, d["id"])
    assert [r["op"] for r in history.list_revisions(ctx, comp, "component_dep", "pg->os")] == ["delete", "update", "create"]
    with pytest.raises(NotFound):
        components.get_dep(ctx, d["id"])


def test_analysis(ctx, comp):
    a = components.analysis(ctx, comp)
    # 宣言がない状態: 式・制約から導いたつながりが「未宣言」として挙がる
    und = {(u["from"], u["to"]): u for u in a["undeclared"]}
    assert set(und) == {("ap", "pg"), ("ap", "os")}
    assert all(u.get("undirected") for u in und.values())  # どちらも制約由来で、向きは決まらない
    assert {l["rule_key"] for l in und[("ap", "os")]["links"]} == {"C-002", "C-003"}
    assert {l["rule_key"] for l in und[("ap", "pg")]["links"]} == {"C-001"}
    assert a["unassigned"]["params"] == ["biz.business_type", "biz.peak_tps", "biz.avg_response_sec", "nfr.safety_factor", "nfr.target_response_sec"]
    assert a["deps"] == [] and a["cycles"] == []

    components.create_dep(ctx, comp, {"from": "ap", "to": "os"})
    components.create_dep(ctx, comp, {"from": "pg", "to": "ap"})   # 制約は逆向きの宣言でも裏付けになる
    components.create_dep(ctx, comp, {"from": "pg", "to": "os"})   # つながりのない宣言
    # 算出式による向きのあるつながり（pg → os）を追加すると、pg → os も裏付けられる
    editing.create_rule(ctx, comp, {"key": "F-OS", "kind": "formula", "target": "os.max_open_files", "expression": "db.max_connections * 100"})
    editing.create_relation(ctx, comp, {"from_ref": "os.max_user_processes", "to_ref": "app.server_count"})
    a = components.analysis(ctx, comp)
    deps = {(d["from_key"], d["to_key"]): d for d in a["deps"]}
    assert all(d["supported"] for d in deps.values())
    assert deps[("pg", "os")]["links"] == [{"kind": "derives", "from_ref": "db.max_connections", "to_ref": "os.max_open_files", "rule_key": "F-OS"}]
    # 手動の関連（os → ap）は、宣言と逆向きなので未宣言として残る
    assert [(u["from"], u["to"], u["links"][0]["kind"]) for u in a["undeclared"]] == [("os", "ap", "influences")]

    by_key = {c["key"]: c for c in a["components"]}
    assert by_key["pg"]["downstream"] == ["ap", "os"] and by_key["os"]["upstream"] == ["ap", "pg"]
    assert (by_key["pg"]["level"], by_key["ap"]["level"], by_key["os"]["level"]) == (0, 1, 2)

    components.create_dep(ctx, comp, {"from": "os", "to": "pg"})
    a = components.analysis(ctx, comp)
    assert a["cycles"] and set(a["cycles"][0]) <= {"ap", "pg", "os"}
    assert all(0 <= c["level"] < 3 for c in a["components"])  # 循環があっても止まる


def test_choice_item_links(ctx, comp):
    items.create_item(ctx, comp, {"layer": "app", "key": "store", "name": "方式", "kind": "choice", "options": ["a", "b"], "selected": "a", "component": "ap"})
    editing.create_rule(ctx, comp, {"key": "F-S", "kind": "formula", "target": "db.reserved_connections", "expression": "10 if app.store == 'a' else 20"})
    a = components.analysis(ctx, comp)
    assert {"kind": "derives", "from_ref": "app.store", "to_ref": "db.reserved_connections", "rule_key": "F-S"} in \
        next(u for u in a["undeclared"] if (u["from"], u["to"]) == ("ap", "pg") and not u.get("undirected"))["links"]


def _approve(ctx, sid, refs):
    analysis.recalc(ctx, sid, "all")
    analysis.apply_proposals(ctx, sid)
    for ref in refs:
        for action in ("skip_ai", "request_review", "approve"):
            param_state.transition(ctx, param(ctx, sid, ref)["id"], action)


def test_notify_flags_downstream_component(ctx, comp):
    _approve(ctx, comp, ["os.max_user_processes", "os.max_open_files", "db.reserved_connections"])
    pg = param(ctx, comp, "db.max_connections")

    # notify なしの依存では、式でつながっていない限りフラグは付かない
    dep = components.create_dep(ctx, comp, {"from": "pg", "to": "os", "note": "カーネルパラメータに波及"})
    editing.update_param(ctx, pg["id"], {"value": 300})
    assert not param(ctx, comp, "os.max_open_files")["flag_upstream_changed"]

    components.update_dep(ctx, dep["id"], {"notify": True})
    editing.update_param(ctx, pg["id"], {"value": 400})
    os_files = param(ctx, comp, "os.max_open_files")
    assert os_files["flag_upstream_changed"] and not os_files["flag_stale"] and os_files["status"] == "approved"
    assert param(ctx, comp, "os.max_user_processes")["flag_upstream_changed"]
    assert not param(ctx, comp, "db.reserved_connections")["flag_upstream_changed"]  # 同じコンポーネント内には付けない
    assert [p["ref"] for p in param_state.approval_queue(ctx, comp)["upstream_changed"]] == ["os.max_user_processes", "os.max_open_files"]
    # 既存の「確認済みにする」で解除できる
    param_state.transition(ctx, os_files["id"], "ack_upstream", "nofile は十分に大きい")
    assert not param(ctx, comp, "os.max_open_files")["flag_upstream_changed"]

    # 構成項目の採用案の変更も波及元になる
    c = items.create_item(ctx, comp, {"layer": "db", "key": "ha", "name": "冗長化方式", "kind": "choice", "options": ["a", "b"], "selected": "a", "component": "pg"})
    items.update_item(ctx, c["id"], {"selected": "b"})
    assert param(ctx, comp, "os.max_open_files")["flag_upstream_changed"]


def test_delete_component(ctx, comp):
    components.create_dep(ctx, comp, {"from": "pg", "to": "os"})
    pg = next(c for c in components.list_components(ctx, comp) if c["key"] == "pg")
    components.delete_component(ctx, pg["id"])
    assert param(ctx, comp, "db.max_connections")["component"] is None  # 項目は残り、未分類に戻る
    assert components.list_deps(ctx, comp) == []
    assert history.list_revisions(ctx, comp, "component", "pg")[0]["before"]["param_count"] == 2


def test_snapshot_baseline_template_export(ctx, comp):
    components.create_dep(ctx, comp, {"from": "pg", "to": "os", "note": "n", "notify": True})
    payload = snapshot.export_payload(ctx, comp)
    assert [c["key"] for c in payload["components"]] == ["ap", "pg", "os"]
    assert payload["component_deps"] == [{"from": "pg", "to": "os", "note": "n", "notify": True}]
    assert next(p for p in payload["params"] if p["key"] == "max_connections")["component"] == "pg"

    clone = templates.clone_system(ctx, comp, "複製")["id"]
    assert snapshot.diff_payloads(payload, snapshot.export_payload(ctx, clone)) == {k: [] for k in snapshot.SECTIONS}
    assert components.list_deps(ctx, clone)[0]["notify"] is True
    t = templates.template_from_system(ctx, comp, "コンポーネントつき")
    assert t["summary"]["components"] == 3 and t["summary"]["component_deps"] == 1

    components.create_component(ctx, clone, {"key": "lb", "name": "LB"})
    editing.update_param(ctx, param(ctx, clone, "app.server_count")["id"], {"component": "lb"})
    d = snapshot.diff_payloads(payload, snapshot.export_payload(ctx, clone))
    assert [(x["key"], x["change"]) for x in d["components"]] == [("lb", "added")]
    assert [x["key"] for x in d["params"]] == ["app.server_count"]

    bad = {**payload, "component_deps": [{"from": "pg", "to": "zz"}]}
    with pytest.raises(Invalid, match="端点"):
        snapshot.replace_with_payload(ctx, clone, bad, keep_status=False, op="update")
    assert "lb" in [c["key"] for c in components.list_components(ctx, clone)]  # 失敗時はロールバックされる

    assert "PostgreSQL" in export.to_csv(ctx, comp).decode("utf-8-sig")
    html = export.to_html(ctx, comp).decode()
    assert "コンポーネントと依存関係" in html and "RHEL" in html
    assert baseline.diff(ctx, comp, "working", "working")["components"] == []


def test_restore_component_assignment(ctx, comp):
    p = param(ctx, comp, "db.max_connections")
    v_pg = versions.timeline(ctx, comp, "param", "db.max_connections")[0]
    editing.update_param(ctx, p["id"], {"component": "os"})
    assert versions.restore_revision(ctx, v_pg["id"])["result"]["component"] == "pg"
    # 記録されたコンポーネントがもう存在しない場合は、所属を変えずに他の内容だけ戻す
    components.delete_component(ctx, next(c for c in components.list_components(ctx, comp) if c["key"] == "pg")["id"])
    editing.update_param(ctx, p["id"], {"component": "os", "value": 1})
    r = versions.restore_revision(ctx, v_pg["id"])["result"]
    assert r["component"] == "os" and r["value"] == 200
