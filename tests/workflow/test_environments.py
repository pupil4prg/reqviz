import pytest

from reqviz.errors import Invalid
from reqviz.repo import history
from reqviz.repo import model as repo
from reqviz.workflow import analysis, baseline, components, editing, environments, items, param_state, snapshot, versions


def param(ctx, sid, ref):
    return repo.params_by_ref(ctx, sid)[ref]


def approve_all(ctx, sid):
    analysis.recalc(ctx, sid, "all")
    analysis.apply_proposals(ctx, sid)
    for p in repo.list_params(ctx, sid):
        for action in ("skip_ai", "request_review", "approve"):
            param_state.transition(ctx, p["id"], action)
    for i in repo.list_items(ctx, sid):
        if i["status"] == "draft":
            items.transition(ctx, i["id"], "request_review")
            items.transition(ctx, i["id"], "approve")


@pytest.fixture
def envs(ctx, web3):
    """本番（web3）と、そこから作った開発環境。"""
    items.create_item(ctx, web3, {"layer": "app", "key": "session_store", "name": "セッション管理方式", "kind": "choice",
                                  "options": ["sticky", "redis"], "selected": "redis"})
    items.create_item(ctx, web3, {"layer": "os", "key": "sysctl", "name": "sysctl", "kind": "document", "content_type": "json", "content": {"somaxconn": 4096}})
    approve_all(ctx, web3)
    dev = environments.create_environment(ctx, web3, {"key": "dev", "name": "開発", "current_key": "prod", "current_name": "本番"})
    return web3, dev["id"]


def test_create_environment(ctx, envs):
    prod, dev = envs
    p, d = repo.get_system(ctx, prod), repo.get_system(ctx, dev)
    assert (p["env_group_id"], p["env_key"], p["env_name"]) == (prod, "prod", "本番")
    assert (d["env_group_id"], d["env_key"], d["env_name"], d["name"]) == (prod, "dev", "開発", p["name"])
    # 構成と値はコピーされ、承認は環境ごとにやり直す
    assert param(ctx, dev, "app.threads_per_server")["value"] == param(ctx, prod, "app.threads_per_server")["value"] == 50
    assert {x["status"] for x in repo.list_params(ctx, dev)} == {"draft"}
    assert {x["status"] for x in repo.list_params(ctx, prod)} == {"approved"}
    assert not any(x["flag_stale"] or x["flag_upstream_changed"] for x in repo.list_params(ctx, dev))
    assert repo.items_by_ref(ctx, dev)["app.session_store"]["selected"] == "redis"

    lst = environments.list_environments(ctx, dev)
    assert [(e["env_key"], e["is_current"], e["approved_count"]) for e in lst] == [("prod", False, 14), ("dev", True, 0)]

    stg = environments.create_environment(ctx, dev, {"key": "stg", "name": "検証"})  # 2 つ目以降は current_* 不要
    assert repo.get_system(ctx, stg["id"])["env_group_id"] == prod
    for bad in ({"key": "dev", "name": "重複"}, {"key": "1x", "name": "n"}, {"key": "qa"}, {"key": "qa", "name": "x" * 33}):
        with pytest.raises(Invalid):
            environments.create_environment(ctx, prod, bad)


def test_first_environment_requires_current_label(ctx, web3):
    assert [e["env_key"] for e in environments.list_environments(ctx, web3)] == [""]
    with pytest.raises(Invalid):
        environments.create_environment(ctx, web3, {"key": "dev", "name": "開発"})
    with pytest.raises(Invalid, match="重複"):
        environments.create_environment(ctx, web3, {"key": "dev", "name": "開発", "current_key": "dev", "current_name": "本番"})
    assert repo.get_system(ctx, web3)["env_group_id"] is None  # 失敗時はグループ化もしない
    with pytest.raises(Invalid, match="環境を分けていません"):
        environments.update_environment(ctx, web3, {"name": "x"})
    with pytest.raises(Invalid, match="他の環境がありません"):
        environments.sync_structure(ctx, web3, [1])


def test_update_and_detach(ctx, envs):
    prod, dev = envs
    assert environments.update_environment(ctx, dev, {"name": "開発（共用）"})["env_name"] == "開発（共用）"
    with pytest.raises(Invalid, match="重複"):
        environments.update_environment(ctx, dev, {"key": "prod"})
    stg = environments.create_environment(ctx, prod, {"key": "stg", "name": "検証"})["id"]
    environments.detach(ctx, stg)
    assert repo.get_system(ctx, stg)["env_group_id"] is None and len(repo.list_params(ctx, stg)) == 14
    assert len(environments.list_environments(ctx, prod)) == 2
    environments.detach(ctx, dev)  # 残り 1 つになったらグループを解消する
    assert repo.get_system(ctx, prod)["env_group_id"] is None and repo.get_system(ctx, prod)["env_key"] == ""


def test_environments_are_independent(ctx, envs):
    prod, dev = envs
    editing.update_param(ctx, param(ctx, dev, "biz.peak_tps")["id"], {"value": 20})
    analysis.recalc(ctx, dev, "stale")
    analysis.apply_proposals(ctx, dev)
    assert param(ctx, dev, "app.threads_per_server")["value"] == 5       # 同じ式を、開発の値で計算
    assert param(ctx, prod, "app.threads_per_server")["value"] == 50
    assert param(ctx, prod, "app.threads_per_server")["status"] == "approved"
    assert history.list_revisions(ctx, prod, "param", "biz.peak_tps")[0]["op"] == "create"  # 履歴も環境ごと
    b = baseline.create_baseline(ctx, prod, "本番 v1")
    assert b["version_no"] == 1 and baseline.list_baselines(ctx, dev) == []


def test_compare(ctx, envs):
    prod, dev = envs
    editing.update_param(ctx, param(ctx, dev, "biz.peak_tps")["id"], {"value": 20})
    editing.update_param(ctx, param(ctx, dev, "app.server_count")["id"], {"unit": "VM"})
    layer = repo.get_layer(ctx, param(ctx, dev, "os.max_open_files")["layer_id"])
    editing.create_param(ctx, dev, {"layer_id": layer["id"], "key": "debug_port", "name": "デバッグポート", "value": 5005})
    items.update_item(ctx, repo.items_by_ref(ctx, dev)["app.session_store"]["id"], {"selected": "sticky"})
    items.update_item(ctx, repo.items_by_ref(ctx, dev)["os.sysctl"]["id"], {"content": {"somaxconn": 128}})
    rule = next(r for r in repo.list_rules(ctx, dev) if r["key"] == "C-002")
    editing.update_rule(ctx, rule["id"], {"severity": "error"})
    editing.delete_rule(ctx, next(r for r in repo.list_rules(ctx, dev) if r["key"] == "N-001")["id"])

    c = environments.compare(ctx, prod)
    assert [e["env_key"] for e in c["environments"]] == ["prod", "dev"]
    rows = {r["ref"]: r for r in c["params"]}
    tps = rows["biz.peak_tps"]
    assert tps["differs"] and tps["values"][str(prod)]["value"] == 200 and tps["values"][str(dev)]["value"] == 20
    assert tps["values"][str(prod)]["status"] == "approved" and tps["missing_in"] == [] and tps["definition_differs"] == []
    assert rows["app.server_count"]["definition_differs"] == ["unit"] and not rows["app.server_count"]["differs"]
    assert rows["os.debug_port"]["missing_in"] == [prod] and list(rows["os.debug_port"]["values"]) == [str(dev)]
    assert not rows["db.max_connections"]["differs"]
    irows = {r["ref"]: r for r in c["items"]}
    assert irows["app.session_store"]["differs"] and irows["app.session_store"]["values"][str(dev)]["value"] == "sticky"
    assert irows["os.sysctl"]["differs"]
    assert [(r["key"], r["missing_in"]) for r in c["rules"]] == [("C-002", []), ("N-001", [dev])]
    # 開発で peak_tps を変えたので、下流の算出値はまだ同じ（要再計算のまま）。値の違いは 3 件
    assert c["summary"] == {"value_differences": 3, "structure_differences": 4}


def test_merge_structure_is_pure():
    src = {"layers": [{"key": "a", "name": "A"}], "components": [], "component_deps": [],
           "params": [{"layer": "a", "key": "n", "name": "新名称", "type": "enum", "enum_options": ["x", "y"], "value": "x", "unit": "", "tags": ["src"], "rationale": "src"},
                      {"layer": "a", "key": "new", "name": "追加", "type": "number", "enum_options": [], "value": 1}],
           "items": [], "rules": [{"key": "R", "kind": "nl", "nl_text": "s"}], "relations": []}
    tgt = {"system": {"name": "T"}, "layers": [{"key": "a", "name": "旧"}, {"key": "b", "name": "B"}],
           "params": [{"layer": "a", "key": "n", "name": "旧名称", "type": "number", "enum_options": [], "value": 5, "status": "approved", "tags": ["tgt"], "rationale": "tgt"},
                      {"layer": "b", "key": "only", "name": "反映先だけ", "type": "number", "enum_options": [], "value": 9, "status": "approved"}],
           "items": [], "rules": [{"key": "R", "kind": "nl", "nl_text": "t"}, {"key": "T-only", "kind": "nl", "nl_text": "x"}], "relations": []}
    m = environments.merge_structure(src, tgt)
    assert [l["key"] for l in m["layers"]] == ["a", "b"] and m["layers"][0]["name"] == "A" and m["system"] == {"name": "T"}
    n = m["params"][0]
    # 型が変わって合わなくなった値は未設定になり、下書きに戻る。根拠とタグは反映先のものを保つ
    assert (n["name"], n["type"], n["value"], n["status"], n["tags"], n["rationale"]) == ("新名称", "enum", None, "draft", ["tgt"], "tgt")
    assert (m["params"][1]["key"], m["params"][1]["status"]) == ("new", "draft")
    assert (m["params"][2]["key"], m["params"][2]["status"]) == ("only", "approved")
    assert [r["key"] for r in m["rules"]] == ["R", "T-only"] and m["rules"][0]["nl_text"] == "s"
    strict = environments.merge_structure(src, tgt, delete_extra=True)
    assert [p["key"] for p in strict["params"]] == ["n", "new"] and [l["key"] for l in strict["layers"]] == ["a"]
    assert [r["key"] for r in strict["rules"]] == ["R"]
    assert tgt["params"][0]["name"] == "旧名称" and src["params"][0]["tags"] == ["src"]  # 入力は変更しない


def test_sync_structure(ctx, envs):
    prod, dev = envs
    approve_all(ctx, dev)
    editing.update_param(ctx, param(ctx, dev, "biz.peak_tps")["id"], {"value": 20, "rationale": "開発は 1/10"})
    analysis.recalc(ctx, dev, "stale")
    analysis.apply_proposals(ctx, dev)
    # 本番側で構成を変更: 名前と別名、パラメータ追加、算出式の変更、選択肢の追加、コンポーネント
    editing.update_param(ctx, param(ctx, prod, "db.max_connections")["id"], {"name": "DB 最大接続数", "aliases": [{"label": "実装名", "value": "max_connections"}]})
    layer = repo.get_layer(ctx, param(ctx, prod, "db.max_connections")["layer_id"])
    editing.create_param(ctx, prod, {"layer_id": layer["id"], "key": "work_mem_mb", "name": "work_mem", "value": 64, "unit": "MB"})
    f2 = next(r for r in repo.list_rules(ctx, prod) if r["key"] == "F-002")
    editing.update_rule(ctx, f2["id"], {"expression": "ceil(app.concurrent_requests / app.server_count) + 10"})
    items.update_item(ctx, repo.items_by_ref(ctx, prod)["app.session_store"]["id"], {"options": ["sticky", "redis", "db"]})
    components.create_component(ctx, prod, {"key": "pg", "name": "PostgreSQL"})
    components.assign(ctx, prod, ["db.max_connections"], "pg")
    dev_only = editing.create_param(ctx, dev, {"layer_id": repo.get_layer(ctx, param(ctx, dev, "os.max_open_files")["layer_id"])["id"], "key": "debug_port", "name": "デバッグ", "value": 5005})
    before_values = {p["ref"]: p["value"] for p in repo.list_params(ctx, dev)}
    rev0 = ctx.conn.execute("SELECT MAX(id) FROM revisions WHERE system_id = ?", (dev,)).fetchone()[0]

    pre = environments.sync_structure(ctx, prod, [dev], preview=True)
    assert pre["preview"] is True and pre["targets"][0]["changes"] == 5
    d = pre["targets"][0]["diff"]
    assert {x["key"]: x["change"] for x in d["params"]} == {"db.max_connections": "modified", "db.work_mem_mb": "added"}
    assert [x["key"] for x in d["rules"]] == ["F-002"] and [x["key"] for x in d["components"]] == ["pg"]
    assert param(ctx, dev, "db.max_connections")["name"] != "DB 最大接続数"  # プレビューでは変更しない

    res = environments.sync_structure(ctx, prod, [dev, dev])
    assert res["targets"][0]["changes"] == 5 and len(res["targets"]) == 1
    after = {p["ref"]: p for p in repo.list_params(ctx, dev)}
    assert after["db.max_connections"]["name"] == "DB 最大接続数" and after["db.max_connections"]["component"] == "pg"
    assert after["db.max_connections"]["aliases"][0]["value"] == "max_connections"
    assert after["db.max_connections"]["status"] == "approved"            # 値が変わらなければ承認は維持
    assert after["db.work_mem_mb"]["value"] == 64 and after["db.work_mem_mb"]["status"] == "draft"  # 新しい項目は下書き
    assert after["os.debug_port"]["id"] != dev_only["id"] and after["os.debug_port"]["value"] == 5005  # 開発だけの項目は残る
    assert {r: p["value"] for r, p in after.items() if r in before_values} == before_values           # 値は開発のまま
    assert after["biz.peak_tps"]["rationale"] == "開発は 1/10"
    # 算出式が変わったパラメータと、その下流だけが要再計算になる
    assert {r for r, p in after.items() if p["flag_stale"]} == {"app.threads_per_server", "db.pool_per_server"}
    assert after["app.threads_per_server"]["value"] == 5
    assert [o["key"] for o in repo.items_by_ref(ctx, dev)["app.session_store"]["options"]] == ["sticky", "redis", "db"]
    assert repo.items_by_ref(ctx, dev)["app.session_store"]["selected"] == "redis"
    assert repo.items_by_ref(ctx, dev)["os.sysctl"]["content"] == {"somaxconn": 4096}

    # 同期は項目ごとの履歴に残り、同期前の状態を再現できる
    assert history.list_revisions(ctx, dev, "param", "db.max_connections")[0]["comment"] == "環境 本番 から構成を同期"
    assert versions.state_at(ctx, dev, f"rev:{rev0}")["params"]["db.max_connections"]["name"] == "DB最大コネクション数"
    assert history.list_audit(ctx, dev)[0]["action"] == "environment.sync"
    assert environments.sync_structure(ctx, prod, [dev])["targets"][0]["changes"] == 0  # 2 回目は差分なし

    strict = environments.sync_structure(ctx, prod, [dev], delete_extra=True)
    assert "os.debug_port" not in repo.params_by_ref(ctx, dev) and strict["targets"][0]["changes"] == 1
    c = environments.compare(ctx, prod)
    assert c["summary"]["structure_differences"] == 0 and c["summary"]["value_differences"] > 0

    for bad in ([prod], [9999], ["x"], []):
        with pytest.raises(Invalid):
            environments.sync_structure(ctx, prod, bad)


def test_copy_values(ctx, envs):
    prod, dev = envs
    approve_all(ctx, dev)
    editing.update_param(ctx, param(ctx, dev, "db.max_connections")["id"], {"value": 500})
    items.update_item(ctx, repo.items_by_ref(ctx, dev)["app.session_store"]["id"], {"selected": "sticky"})
    items.update_item(ctx, repo.items_by_ref(ctx, dev)["os.sysctl"]["id"], {"content": {"somaxconn": 8192}})
    editing.create_param(ctx, dev, {"layer_id": repo.get_layer(ctx, param(ctx, dev, "os.max_open_files")["layer_id"])["id"], "key": "x", "name": "x", "value": 1})

    res = environments.copy_values(ctx, dev, prod, ["db.max_connections", "app.session_store", "os.sysctl", "biz.peak_tps", "app.threads_per_server", "os.x"])
    assert res["copied"] == ["db.max_connections", "app.session_store", "os.sysctl"]
    assert {s["ref"]: s["reason"][:6] for s in res["skipped"]} == {"biz.peak_tps": "値が同じ", "app.threads_per_server": "算出式で決ま", "os.x": "どちらかの環"}
    p = param(ctx, prod, "db.max_connections")
    assert p["value"] == 500 and p["status"] == "draft"  # 反映先では通常の変更として扱う
    assert history.list_revisions(ctx, prod, "param", "db.max_connections")[0]["comment"] == "環境 開発 から値をコピー"
    assert repo.items_by_ref(ctx, prod)["app.session_store"]["selected"] == "sticky"
    assert repo.items_by_ref(ctx, prod)["os.sysctl"]["content"] == {"somaxconn": 8192}
    with pytest.raises(Invalid):
        environments.copy_values(ctx, dev, dev, ["db.max_connections"])
    with pytest.raises(Invalid):
        environments.copy_values(ctx, dev, prod, [])


def test_snapshot_is_unaffected(ctx, envs):
    prod, dev = envs
    assert snapshot.diff_payloads(snapshot.export_payload(ctx, prod, include_status=False), snapshot.export_payload(ctx, dev, include_status=False)) == \
        {k: [] for k in snapshot.SECTIONS}
