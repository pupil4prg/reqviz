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
    cfg = Config(host="127.0.0.1", port=0, db_path=tmp_path / "api.db", templates_dir=ROOT / "templates",
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
