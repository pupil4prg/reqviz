import json
import threading
import urllib.error
import urllib.request

import pytest

from reqviz.config import Config, LLMConfig
from reqviz.server import make_server

from ..conftest import ROOT


@pytest.fixture
def server(tmp_path):
    cfg = Config(host="127.0.0.1", port=0, db_path=tmp_path / "api.db", templates_dir=ROOT / "templates", auth_enabled=False,
                 llm=LLMConfig())
    srv = make_server(cfg, run_ai_in_thread=False)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def call(base, method, path, body=None, raw=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "X-Reqviz-User": "alice"})
    try:
        with urllib.request.urlopen(req) as res:
            content = res.read()
            return res.status, (content if raw else json.loads(content)), res.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def test_static_and_404(server):
    status, body, headers = call(server, "GET", "/", raw=True)
    assert status == 200 and b"<html" in body.lower()
    assert call(server, "GET", "/../pyproject.toml", raw=True)[0] == 404
    assert call(server, "GET", "/api/nope")[0] == 404
    assert call(server, "DELETE", "/api/systems")[0] == 405


def test_end_to_end(server):
    st, meta, _ = call(server, "GET", "/api/meta")
    assert meta["user"] == "alice" and meta["llm"]["enabled"] is False

    st, tpls, _ = call(server, "GET", "/api/templates")
    assert [t["name"] for t in tpls] == ["Web三層"]

    st, s, _ = call(server, "POST", "/api/systems", {"name": "販売管理", "template_id": tpls[0]["id"]})
    assert st == 200
    sid = s["id"]

    st, g, _ = call(server, "GET", f"/api/systems/{sid}/graph")
    assert len(g["nodes"]) == 14 and g["baseline_changes"] is None
    assert all("x" in n for n in g["nodes"])

    st, res, _ = call(server, "POST", f"/api/systems/{sid}/recalc", {"mode": "stale"})
    assert {r["ref"] for r in res if r["status"] == "proposed"} >= {"app.threads_per_server"}
    st, applied, _ = call(server, "POST", f"/api/systems/{sid}/apply-proposals", {})
    assert len(applied) == 4

    params = {p["ref"]: p for p in call(server, "GET", f"/api/systems/{sid}/params")[1]}
    p = params["biz.peak_tps"]
    st, upd, _ = call(server, "PUT", f"/api/params/{p['id']}", {"value": 300, "row_version": p["row_version"]})
    assert st == 200 and upd["value"] == 300
    st, err, _ = call(server, "PUT", f"/api/params/{p['id']}", {"value": 301, "row_version": p["row_version"]})
    assert st == 409 and err["error"]["code"] == "conflict"
    st, err, _ = call(server, "PUT", f"/api/params/{p['id']}", {"value": "abc"})
    assert st == 422

    st, detail, _ = call(server, "GET", f"/api/params/{p['id']}")
    assert detail["revisions"][0]["actor"] == "alice"

    # 一括で AI スキップ → レビュー依頼（要再計算のものは失敗）
    ids = [x["id"] for x in params.values()]
    st, bulk, _ = call(server, "POST", f"/api/systems/{sid}/transition", {"param_ids": ids, "action": "skip_ai"})
    assert len(bulk["ok"]) == 14
    st, bulk, _ = call(server, "POST", f"/api/systems/{sid}/transition", {"param_ids": ids, "action": "request_review"})
    assert bulk["failed"] and all("reasons" in f["details"] for f in bulk["failed"])

    st, v, _ = call(server, "POST", f"/api/systems/{sid}/rules/validate", {"key": "Z", "kind": "constraint", "expression": "app.nope > 1"})
    assert v["ok"] is False and "未定義" in v["errors"][0]
    st, rule, _ = call(server, "POST", f"/api/systems/{sid}/rules", {"key": "Z", "kind": "constraint", "expression": "app.server_count > 1"})
    assert st == 200
    st, rules, _ = call(server, "GET", f"/api/systems/{sid}/rules")
    assert any(x["key"] == "Z" and x["errors"] == [] for x in rules)

    st, bl, _ = call(server, "GET", f"/api/systems/{sid}/baselines")
    assert bl["readiness"]["ready"] is False
    st, err, _ = call(server, "POST", f"/api/systems/{sid}/baselines", {"label": "x"})
    assert st == 422 and err["error"]["details"]["reasons"]

    st, audit, _ = call(server, "GET", f"/api/systems/{sid}/audit")
    assert any(a["action"] == "param.skip_ai" for a in audit)

    st, d, _ = call(server, "GET", f"/api/systems/{sid}/diff?from=working&to=working")
    assert d["params"] == []


def test_exports_and_import(server):
    tpl = call(server, "GET", "/api/templates")[1][0]
    sid = call(server, "POST", "/api/systems", {"name": "E", "template_id": tpl["id"]})[1]["id"]
    st, body, headers = call(server, "GET", f"/api/systems/{sid}/export?format=csv", raw=True)
    assert body.startswith(b"\xef\xbb\xbf") and "attachment" in headers["Content-Disposition"]
    assert "参照名" in body.decode("utf-8-sig").splitlines()[0]
    st, body, _ = call(server, "GET", f"/api/systems/{sid}/export?format=html", raw=True)
    assert b"<svg" in body and "Web三層".encode() not in body[:0]
    st, body, _ = call(server, "GET", f"/api/systems/{sid}/export?format=json", raw=True)
    payload = json.loads(body)
    st, rules_body, _ = call(server, "GET", f"/api/systems/{sid}/export?format=rules-json", raw=True)
    assert json.loads(rules_body)["format"] == "reqviz/rules"
    assert call(server, "GET", f"/api/systems/{sid}/export?format=xls")[0] == 400

    sid2 = call(server, "POST", "/api/systems", {"name": "空"})[1]["id"]
    st, res, _ = call(server, "POST", f"/api/systems/{sid2}/import", {"payload": payload})
    assert st == 200
    assert len(call(server, "GET", f"/api/systems/{sid2}/params")[1]) == 14
    st, res, _ = call(server, "POST", f"/api/systems/{sid2}/import", {"payload": json.loads(rules_body)})
    assert res == {"created": [], "updated": []}

    st, t2, _ = call(server, "POST", "/api/templates", {"system_id": sid2, "name": "Web三層"})
    assert t2["version"] == 2
    st, body, _ = call(server, "GET", f"/api/templates/{t2['id']}/export", raw=True)
    assert "status" not in json.loads(body)["params"][0]


def test_ai_not_configured(server):
    sid = call(server, "POST", "/api/systems", {"name": "A"})[1]["id"]
    st, err, _ = call(server, "POST", f"/api/systems/{sid}/ai/evaluate", {})
    assert st == 422 and "LLM" in err["error"]["message"]
    st, s, _ = call(server, "PUT", "/api/settings/llm", {"base_url": "http://localhost:4000/v1/", "model": "m", "timeout": 5})
    assert s["enabled"] and s["base_url"] == "http://localhost:4000/v1" and s["api_key_set"] is False


def test_bad_json(server):
    req = urllib.request.Request(server + "/api/systems", data=b"{bad", method="POST")
    with pytest.raises(urllib.error.HTTPError) as ei:
        urllib.request.urlopen(req)
    assert ei.value.code == 400


def test_catalog_and_meta(server):
    st, cat, _ = call(server, "GET", "/api")
    routes = {(r["method"], r["path"]) for r in cat["routes"]}
    assert ("POST", "/api/systems/{sid}/items") in routes and ("POST", "/api/revisions/{id}/restore") in routes
    assert cat["agent_endpoints"]["mcp"] == "/mcp" and all(r["summary"] for r in cat["routes"])
    meta = call(server, "GET", "/api/meta")[1]
    assert meta["item_kinds"] == {"choice": "選択肢", "document": "不定形データ"}


def test_items_and_history_api(server):
    tpl = call(server, "GET", "/api/templates")[1][0]
    sid = call(server, "POST", "/api/systems", {"name": "I", "template_id": tpl["id"]})[1]["id"]

    st, c, _ = call(server, "POST", f"/api/systems/{sid}/items", {
        "layer": "app", "key": "deploy", "name": "デプロイ方式", "kind": "choice",
        "options": [{"key": "rolling", "label": "ローリング"}, {"key": "bluegreen", "label": "Blue/Green", "cons": ["費用が 2 倍"]}],
    })
    assert st == 200 and c["selected"] is None and c["ref"] == "app.deploy"
    st, d, _ = call(server, "POST", f"/api/systems/{sid}/items", {
        "layer": "os", "key": "hosts", "name": "hosts 定義", "kind": "document", "content_type": "yaml", "content": "web: 10.0.0.1\n"})
    assert st == 200
    assert call(server, "POST", f"/api/systems/{sid}/items", {"layer": "os", "key": "hosts", "name": "x", "kind": "document"})[0] == 422
    assert [i["key"] for i in call(server, "GET", f"/api/systems/{sid}/items?kind=document")[1]] == ["hosts"]
    assert len(call(server, "GET", f"/api/systems/{sid}/items")[1]) == 2

    st, err, _ = call(server, "POST", f"/api/items/{c['id']}/transition", {"action": "request_review"})
    assert st == 422 and err["error"]["details"]["reasons"]
    st, c2, _ = call(server, "PUT", f"/api/items/{c['id']}", {"selected": "rolling", "comment": "停止時間を許容", "row_version": c["row_version"]})
    assert c2["selected"] == "rolling"
    assert call(server, "PUT", f"/api/items/{c['id']}", {"selected": "bluegreen", "row_version": c["row_version"]})[0] == 409
    assert call(server, "POST", f"/api/items/{c['id']}/transition", {"action": "request_review"})[1]["status"] == "in_review"
    assert [i["ref"] for i in call(server, "GET", f"/api/systems/{sid}/approvals")[1]["items"]] == ["app.deploy"]
    assert call(server, "POST", f"/api/items/{c['id']}/transition", {"action": "approve"})[1]["status"] == "approved"

    st, detail, _ = call(server, "GET", f"/api/systems/{sid}/items/by-ref/app.deploy")
    assert [r["version"] for r in detail["revisions"]] == [2, 1] and detail["review_blockers"] == []
    assert any(a["action"] == "item.approve" for a in detail["audit"])
    assert call(server, "GET", f"/api/systems/{sid}/items/by-ref/app.none")[0] == 404
    assert call(server, "GET", f"/api/systems/{sid}/params/by-ref/app.server_count")[1]["value"] == 3

    # 履歴: 版の系列 → 時点の再現 → 差分 → 復元
    st, tl, _ = call(server, "GET", f"/api/systems/{sid}/history/timeline?entity=item&key=app.deploy")
    v1 = tl[-1]
    assert call(server, "GET", f"/api/revisions/{v1['id']}")[1]["after"]["selected"] is None
    st, state, _ = call(server, "GET", f"/api/systems/{sid}/history/state?at=rev:{v1['id']}")
    assert state["items"]["app.deploy"]["selected"] is None and "os.hosts" not in state["items"]
    st, diff, _ = call(server, "GET", f"/api/systems/{sid}/history/diff?from=rev:{v1['id']}")
    assert {(x["key"], x["change"]) for x in diff["items"]} == {("app.deploy", "modified"), ("os.hosts", "added")}
    assert call(server, "GET", f"/api/systems/{sid}/history/state?at=bogus")[0] == 422
    assert call(server, "GET", f"/api/systems/{sid}/history/timeline?entity=x&key=y")[0] == 422
    st, res, _ = call(server, "POST", f"/api/revisions/{v1['id']}/restore", {"comment": "決定を取り消し"})
    assert res["result"]["selected"] is None and res["result"]["status"] == "draft"
    assert call(server, "POST", "/api/revisions/999999/restore", {})[0] == 404

    assert call(server, "DELETE", f"/api/items/{d['id']}")[1] == {"ok": True}
    assert call(server, "GET", f"/api/items/{d['id']}")[0] == 404
    assert any(a["action"] == "system.create" for a in call(server, "GET", "/api/audit")[1])

    # 書き出しにも構成項目が含まれる
    st, body, _ = call(server, "GET", f"/api/systems/{sid}/export?format=json", raw=True)
    assert json.loads(body)["items"][0]["key"] == "deploy"
    st, body, _ = call(server, "GET", f"/api/systems/{sid}/export?format=html", raw=True)
    assert "デプロイ方式" in body.decode()

    rel = call(server, "GET", f"/api/systems/{sid}/relations")[1][0]
    assert call(server, "PUT", f"/api/relations/{rel['id']}", {"note": "更新"})[1]["note"] == "更新"
    assert call(server, "GET", f"/api/relations/{rel['id']}")[1]["note"] == "更新"
    rule = call(server, "GET", f"/api/systems/{sid}/rules")[1][0]
    assert call(server, "GET", f"/api/rules/{rule['id']}")[1]["revisions"][0]["version"] == 1
    layer = call(server, "GET", f"/api/systems/{sid}/layers")[1][0]
    assert call(server, "GET", f"/api/layers/{layer['id']}")[1]["key"] == layer["key"]
