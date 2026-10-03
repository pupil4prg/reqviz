import pytest

from reqviz import db
from reqviz.errors import Conflict, Invalid, NotFound
from reqviz.repo import history
from reqviz.repo import model as repo
from reqviz.workflow import analysis, baseline, editing, param_state, snapshot, templates


def pid(ctx, sid, ref):
    return repo.params_by_ref(ctx, sid)[ref]["id"]


def recalc_apply_all(ctx, sid):
    analysis.recalc(ctx, sid, "all")
    analysis.apply_proposals(ctx, sid)


def approve_all(ctx, sid):
    for p in repo.list_params(ctx, sid):
        if p["status"] == "draft":
            param_state.transition(ctx, p["id"], "skip_ai")
        param_state.transition(ctx, p["id"], "request_review")
        param_state.transition(ctx, p["id"], "approve")


# ------------------------------------------------------------ migration / system

def test_migrate_idempotent(conn):
    assert db.migrate(conn) == []


def test_system_crud_and_optimistic_lock(ctx):
    s = editing.create_system(ctx, {"name": "A"})
    s2 = editing.update_system(ctx, s["id"], {"name": "B", "row_version": s["row_version"]})
    assert s2["name"] == "B"
    with pytest.raises(Conflict):
        editing.update_system(ctx, s["id"], {"name": "C", "row_version": s["row_version"]})
    with pytest.raises(Invalid):
        editing.create_system(ctx, {"name": " "})
    editing.delete_system(ctx, s["id"])
    with pytest.raises(NotFound):
        repo.get_system(ctx, s["id"])


# ------------------------------------------------------------ template -> system

def test_template_creates_system_with_stale_formulas(ctx, web3):
    params = repo.params_by_ref(ctx, web3)
    assert len(params) == 14
    assert params["app.threads_per_server"]["flag_stale"]
    assert not params["biz.peak_tps"]["flag_stale"]
    assert all(p["status"] == "draft" for p in params.values())
    s = repo.get_system(ctx, web3)
    assert s["template_id"] and s["template_version"] == 1


def test_template_update_not_propagated(ctx, web3, web3_payload):
    web3_payload["params"][0]["name"] = "変更後"
    t2 = templates.save_template(ctx, "Web三層", "", web3_payload)
    assert t2["version"] == 2
    assert repo.params_by_ref(ctx, web3)["biz.business_type"]["name"] == "業務特性"


def test_import_template_rejects_bad_rules(ctx, web3_payload):
    web3_payload["rules"][0]["expression"] = "biz.nope + 1"
    with pytest.raises(Invalid):
        templates.import_template(ctx, web3_payload)
    assert ctx.conn.execute("SELECT COUNT(*) FROM systems").fetchone()[0] == 0


# ------------------------------------------------------------ recalc

def test_recalc_and_apply(ctx, web3):
    res = {r["ref"]: r for r in analysis.recalc(ctx, web3, "stale")}
    assert res["app.concurrent_requests"]["new_value"] == 150  # ceil(200*0.5*1.5)
    assert res["app.threads_per_server"]["new_value"] == 50
    assert res["db.pool_per_server"]["new_value"] == 25
    p = repo.params_by_ref(ctx, web3)
    assert p["app.threads_per_server"]["has_proposal"] and p["app.threads_per_server"]["value"] is None

    applied = analysis.apply_proposals(ctx, web3)
    assert len(applied) == 4
    p = repo.params_by_ref(ctx, web3)
    assert p["db.pool_per_server"]["value"] == 25
    assert not any(x["flag_stale"] for x in p.values())
    assert p["db.pool_per_server"]["source"] == "formula"


def test_value_change_flags_downstream(ctx, web3):
    recalc_apply_all(ctx, web3)
    approve_all(ctx, web3)
    p = repo.get_param(ctx, pid(ctx, web3, "biz.peak_tps"))
    after = editing.update_param(ctx, p["id"], {"value": 400, "row_version": p["row_version"]})
    assert after["status"] == "draft"  # 自身は値変更で下書きへ
    params = repo.params_by_ref(ctx, web3)
    for ref in ("app.concurrent_requests", "app.threads_per_server", "db.pool_per_server"):
        assert params[ref]["flag_stale"] and params[ref]["flag_upstream_changed"]
        assert params[ref]["status"] == "approved"  # 自動では戻さない
    assert not params["db.pool_ratio"]["flag_stale"]

    analysis.recalc(ctx, web3, "stale")
    analysis.apply_proposals(ctx, web3, ["app.concurrent_requests"])
    params = repo.params_by_ref(ctx, web3)
    assert params["app.concurrent_requests"]["status"] == "draft"  # 承認済み→下書き
    assert params["app.concurrent_requests"]["value"] == 300
    assert params["app.threads_per_server"]["flag_stale"]  # 下流は要再計算のまま
    actions = [a["action"] for a in history.list_audit(ctx, web3)]
    assert "param.revert_to_draft" in actions


def test_recalc_unchanged_clears_stale(ctx, web3):
    recalc_apply_all(ctx, web3)
    p = repo.get_param(ctx, pid(ctx, web3, "biz.peak_tps"))
    editing.update_param(ctx, p["id"], {"value": 200})  # 同じ値 → 変更なし
    assert not repo.params_by_ref(ctx, web3)["app.threads_per_server"]["flag_stale"]
    editing.update_param(ctx, p["id"], {"value": 201})
    editing.update_param(ctx, p["id"], {"value": 200})
    res = analysis.recalc(ctx, web3, "stale")
    assert all(r["status"] == "unchanged" for r in res)
    assert not any(x["flag_stale"] for x in repo.list_params(ctx, web3))


def test_recalc_error_recorded(ctx, web3):
    p = repo.get_param(ctx, pid(ctx, web3, "app.server_count"))
    editing.update_param(ctx, p["id"], {"value": 0})
    res = {r["ref"]: r for r in analysis.recalc(ctx, web3, "all")}
    assert res["app.threads_per_server"]["status"] == "error"
    assert "0 で除算" in repo.params_by_ref(ctx, web3)["app.threads_per_server"]["proposed_error"]


def test_recalc_bad_mode(ctx, web3):
    with pytest.raises(Invalid):
        analysis.recalc(ctx, web3, "nope")
    with pytest.raises(Invalid):
        analysis.recalc(ctx, web3, "downstream", ["x.y"])


# ------------------------------------------------------------ violations / graph

def test_violations_and_graph(ctx, web3):
    recalc_apply_all(ctx, web3)
    p = repo.get_param(ctx, pid(ctx, web3, "db.max_connections"))
    editing.update_param(ctx, p["id"], {"value": 50})
    viols = {v.rule_key: v for v in analysis.violations(ctx, web3)}
    assert viols["C-001"].severity == "error"
    g = analysis.graph(ctx, web3)
    nodes = {n["ref"]: n for n in g["nodes"]}
    assert nodes["db.max_connections"]["violation"] == "error"
    assert nodes["app.threads_per_server"]["formula_rule"] == "F-002"
    assert nodes["db.pool_per_server"]["nl_rules"] == ["N-001"]
    kinds = {e["kind"] for e in g["edges"]}
    assert kinds == {"derives", "constrains", "influences"}
    assert any(rn["key"] == "C-001" and rn["layer_key"] == "db" for rn in g["rule_nodes"])
    assert g["rule_errors"] == {}


# ------------------------------------------------------------ params / layers / rules editing

def test_param_validation(ctx, web3):
    layer = repo.list_layers(ctx, web3)[0]
    with pytest.raises(Invalid):
        editing.create_param(ctx, web3, {"layer_id": layer["id"], "key": "1bad", "name": "x"})
    with pytest.raises(Invalid):
        editing.create_param(ctx, web3, {"layer_id": layer["id"], "key": "peak_tps", "name": "dup"})
    with pytest.raises(Invalid):
        editing.create_param(ctx, web3, {"layer_id": layer["id"], "key": "e", "name": "e", "type": "enum"})
    p = editing.create_param(ctx, web3, {"layer_id": layer["id"], "key": "n", "name": "n", "value": "12"})
    assert p["value"] == 12
    with pytest.raises(Invalid):
        editing.update_param(ctx, p["id"], {"value": "abc"})


def test_delete_param_referenced(ctx, web3):
    with pytest.raises(Invalid) as ei:
        editing.delete_param(ctx, pid(ctx, web3, "biz.peak_tps"))
    assert "F-001" in ei.value.details["rules"]


def test_rename_layer_and_param_updates_rules(ctx, web3):
    layer = next(l for l in repo.list_layers(ctx, web3) if l["key"] == "biz")
    editing.update_layer(ctx, layer["id"], {"key": "business"})
    p = repo.get_param(ctx, pid(ctx, web3, "business.peak_tps"))
    editing.update_param(ctx, p["id"], {"key": "tps"})
    rules = {r["key"]: r for r in repo.list_rules(ctx, web3)}
    assert "business.tps" in rules["F-001"]["expression"]
    assert "business.business_type" in rules["F-003"]["expression"]
    assert analysis.graph(ctx, web3)["rule_errors"] == {}


def test_layer_delete_and_reorder(ctx, web3):
    layers = repo.list_layers(ctx, web3)
    with pytest.raises(Invalid):
        editing.delete_layer(ctx, layers[0]["id"])
    new = editing.create_layer(ctx, web3, {"key": "net", "name": "ネットワーク"})
    ids = [new["id"]] + [l["id"] for l in layers]
    assert [l["key"] for l in editing.reorder_layers(ctx, web3, ids)][0] == "net"
    with pytest.raises(Invalid):
        editing.reorder_layers(ctx, web3, ids[:-1])
    editing.delete_layer(ctx, new["id"])


def test_rule_crud_and_validation(ctx, web3):
    with pytest.raises(Invalid) as ei:
        editing.create_rule(ctx, web3, {"key": "X", "kind": "formula", "target": "app.server_count", "expression": "app.threads_per_server"})
    assert any("循環" in m for m in ei.value.details["errors"])
    with pytest.raises(Invalid):
        editing.create_rule(ctx, web3, {"key": "F-001", "kind": "constraint", "expression": "1 > 0"})
    r = editing.create_rule(ctx, web3, {"key": "C-9", "kind": "constraint", "expression": "app.server_count >= 2", "severity": "warning"})
    r = editing.update_rule(ctx, r["id"], {"expression": "app.server_count >= 4", "row_version": r["row_version"]})
    assert any(v.rule_key == "C-9" for v in analysis.violations(ctx, web3))
    editing.delete_rule(ctx, r["id"])
    revs = history.list_revisions(ctx, web3, "rule", "C-9")
    assert [x["op"] for x in revs] == ["delete", "update", "create"]
    assert revs[1]["changes"][0]["field"] == "expression"


def test_formula_rule_change_marks_stale(ctx, web3):
    recalc_apply_all(ctx, web3)
    r = next(x for x in repo.list_rules(ctx, web3) if x["key"] == "F-003")
    editing.update_rule(ctx, r["id"], {"expression": "0.4"})
    params = repo.params_by_ref(ctx, web3)
    assert params["db.pool_ratio"]["flag_stale"] and params["db.pool_per_server"]["flag_stale"]
    editing.update_rule(ctx, r["id"], {"enabled": False})
    assert not repo.params_by_ref(ctx, web3)["db.pool_ratio"]["flag_stale"]


def test_relations(ctx, web3):
    rel = editing.create_relation(ctx, web3, {"from_ref": "biz.peak_tps", "to_ref": "app.server_count"})
    with pytest.raises(Invalid):
        editing.create_relation(ctx, web3, {"from_ref": "biz.peak_tps", "to_ref": "app.server_count"})
    with pytest.raises(Invalid):
        editing.create_relation(ctx, web3, {"from_ref": "biz.peak_tps", "to_ref": "nope.x"})
    editing.delete_relation(ctx, rel["id"])


# ------------------------------------------------------------ param state

def test_param_transitions(ctx, web3):
    p_id = pid(ctx, web3, "biz.peak_tps")
    with pytest.raises(Invalid):
        param_state.transition(ctx, p_id, "approve")
    p = param_state.transition(ctx, p_id, "skip_ai")
    assert p["status"] == "ai_evaluated" and p["ai_skipped"]
    param_state.transition(ctx, p_id, "request_review")
    with pytest.raises(Invalid):
        param_state.transition(ctx, p_id, "reject")  # コメント必須
    p = param_state.transition(ctx, p_id, "reject", "根拠を追記して")
    assert p["status"] == "draft"
    with pytest.raises(Invalid):
        param_state.transition(ctx, p_id, "fly")
    log = history.list_audit(ctx, web3, "biz.peak_tps")
    assert log[0]["action"] == "param.reject" and log[0]["comment"] == "根拠を追記して"


def test_request_review_blocked_by_stale(ctx, web3):
    p_id = pid(ctx, web3, "app.threads_per_server")
    param_state.transition(ctx, p_id, "skip_ai")
    with pytest.raises(Invalid) as ei:
        param_state.transition(ctx, p_id, "request_review")
    assert any("要再計算" in r for r in ei.value.details["reasons"])


def test_ack_upstream(ctx, web3):
    recalc_apply_all(ctx, web3)
    approve_all(ctx, web3)
    p = repo.get_param(ctx, pid(ctx, web3, "biz.peak_tps"))
    editing.update_param(ctx, p["id"], {"value": 210})
    t = pid(ctx, web3, "app.concurrent_requests")
    with pytest.raises(Invalid):
        param_state.transition(ctx, t, "ack_upstream")
    q = param_state.approval_queue(ctx, web3)
    assert {x["ref"] for x in q["upstream_changed"]} >= {"app.concurrent_requests"}
    p = param_state.transition(ctx, t, "ack_upstream", "影響なしを確認")
    assert p["status"] == "approved" and not p["flag_upstream_changed"]


# ------------------------------------------------------------ baselines

def test_baseline_flow(ctx, web3):
    st = baseline.readiness(ctx, web3)
    assert not st["ready"] and st["blockers"]
    with pytest.raises(Invalid):
        baseline.create_baseline(ctx, web3)

    recalc_apply_all(ctx, web3)
    approve_all(ctx, web3)
    # nproc を小さくして warning を発生させる
    p = repo.get_param(ctx, pid(ctx, web3, "os.max_user_processes"))
    editing.update_param(ctx, p["id"], {"value": 10})
    param_state.transition(ctx, p["id"], "skip_ai")
    param_state.transition(ctx, p["id"], "request_review")
    param_state.transition(ctx, p["id"], "approve")

    st = baseline.readiness(ctx, web3)
    assert st["ready"] and [w["rule_key"] for w in st["warnings"]] == ["C-002"]
    b = baseline.create_baseline(ctx, web3, "初版")
    assert b["version_no"] == 1 and b["status"] == "in_review"
    with pytest.raises(Invalid):
        baseline.create_baseline(ctx, web3)  # レビュー中の版がある
    with pytest.raises(Invalid):
        baseline.approve_baseline(ctx, b["id"])  # warning があるのでコメント必須
    b = baseline.approve_baseline(ctx, b["id"], "nproc は別途見直す")
    assert b["status"] == "approved"
    assert repo.get_system(ctx, web3)["current_baseline_id"] == b["id"]
    assert baseline.changed_since_current(ctx, web3) == []

    # 変更 → 差分 → 復元
    p = repo.get_param(ctx, pid(ctx, web3, "biz.peak_tps"))
    editing.update_param(ctx, p["id"], {"value": 999})
    d = baseline.diff(ctx, web3, "current", "working")
    changed = {x["key"]: x for x in d["params"]}
    assert "biz.peak_tps" in changed
    assert {f["field"] for f in changed["biz.peak_tps"]["fields"]} == {"value", "status"}
    assert baseline.changed_since_current(ctx, web3) == ["biz.peak_tps"]

    baseline.restore_baseline(ctx, b["id"])
    params = repo.params_by_ref(ctx, web3)
    assert params["biz.peak_tps"]["value"] == 200 and params["biz.peak_tps"]["status"] == "approved"
    assert not any(x["flag_stale"] or x["flag_upstream_changed"] for x in params.values())
    assert baseline.diff(ctx, web3, "current", "working")["params"] == []


def test_baseline_reject(ctx, web3):
    recalc_apply_all(ctx, web3)
    approve_all(ctx, web3)
    b = baseline.create_baseline(ctx, web3)
    with pytest.raises(Invalid):
        baseline.reject_baseline(ctx, b["id"])
    b = baseline.reject_baseline(ctx, b["id"], "再検討")
    assert b["status"] == "rejected"
    with pytest.raises(Invalid):
        baseline.approve_baseline(ctx, b["id"], "x")
    assert baseline.create_baseline(ctx, web3)["version_no"] == 2


# ------------------------------------------------------------ snapshot / rules io

def test_export_import_roundtrip(ctx, web3):
    payload = snapshot.export_payload(ctx, web3)
    s = editing.create_system(ctx, {"name": "copy"})
    snapshot.replace_with_payload(ctx, s["id"], payload, keep_status=True, op="create")
    again = snapshot.export_payload(ctx, s["id"])
    again["system"] = payload["system"]
    assert again == payload


def test_rules_import(ctx, web3):
    rules = snapshot.export_rules(ctx, web3)
    s = templates.clone_system(ctx, web3, "clone")
    rules["rules"][0]["description"] = "変更"
    rules["rules"].append({"key": "C-NEW", "kind": "constraint", "expression": "app.server_count >= 1"})
    res = snapshot.import_rules(ctx, s["id"], rules)
    assert res == {"created": ["C-NEW"], "updated": ["C-001"]}
    with pytest.raises(Invalid):
        snapshot.import_rules(ctx, s["id"], {"format": "x"})


def test_clone_keeps_flags(ctx, web3):
    s = templates.clone_system(ctx, web3, "")
    assert repo.params_by_ref(ctx, s["id"])["app.threads_per_server"]["flag_stale"]
    assert repo.get_system(ctx, s["id"])["name"].endswith("のコピー")
