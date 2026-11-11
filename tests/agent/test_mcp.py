import base64
import io
import json

from reqviz.agent.tools import TOOLS, HttpClient, call_tool
from reqviz.mcp.__main__ import serve
from reqviz.mcp.protocol import McpServer

from .conftest import http, make_system

V = "2026-07-28"
META = {
    "io.modelcontextprotocol/protocolVersion": V,
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def rpc(base, method, params=None, *, headers=None, meta=True, rid=1, name=None):
    """新方式（2026-07-28）のリクエストを送る。"""
    params = dict(params or {})
    if meta:
        params["_meta"] = META
    h = {"MCP-Protocol-Version": V, "Mcp-Method": method, "Accept": "application/json, text/event-stream"}
    if method == "tools/call":
        h["Mcp-Name"] = name or params.get("name", "")
    h.update(headers or {})
    h = {k: v for k, v in h.items() if v is not None}
    return http(base, "POST", "/mcp", {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}, h)


def test_tool_table_is_consistent():
    names = [t.name for t in TOOLS]
    assert len(names) == len(set(names))
    for t in TOOLS:
        schema = t.input_schema
        assert schema["type"] == "object"
        assert set(schema.get("required", [])) <= set(t.props), t.name
        assert set(t.query) <= set(t.props), t.name
        assert t.read_only or t.method != "GET", t.name


def test_discover_and_list(server):
    st, res, _ = rpc(server, "server/discover")
    r = res["result"]
    assert st == 200 and r["supportedVersions"][0] == V and "2025-11-25" in r["supportedVersions"]
    assert r["capabilities"] == {"tools": {}} and r["resultType"] == "complete"
    assert r["cacheScope"] == "public" and r["ttlMs"] > 0
    assert r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "reqviz"

    st, res, _ = rpc(server, "tools/list")
    r = res["result"]
    assert [t["name"] for t in r["tools"]] == [t.name for t in TOOLS]
    assert r["resultType"] == "complete" and "ttlMs" in r and r["cacheScope"] == "public"
    by_name = {t["name"]: t for t in r["tools"]}
    assert by_name["list_params"]["annotations"]["readOnlyHint"] is True
    assert by_name["delete_system"]["annotations"] == {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False}
    assert by_name["create_item"]["inputSchema"]["required"] == ["system_id", "key", "name", "kind"]


def test_tools_call_roundtrip(server):
    sid = make_system(server)
    st, res, _ = rpc(server, "tools/call", {"name": "list_systems"})
    r = res["result"]
    assert r["structuredContent"][0]["name"] == "販売管理" and json.loads(r["content"][0]["text"]) == r["structuredContent"]
    assert "isError" not in r and r["resultType"] == "complete"

    # 列挙データと不定形データを MCP 経由で作成・変更する
    args = {"system_id": sid, "layer": "app", "key": "session_store", "name": "セッション管理", "kind": "choice",
            "options": [{"key": "sticky"}, {"key": "redis", "pros": ["無停止で切り替え可"]}], "selected": "sticky"}
    st, res, _ = rpc(server, "tools/call", {"name": "create_item", "arguments": args}, headers={"X-Reqviz-User": "agent-1"})
    item = res["result"]["structuredContent"]
    assert item["ref"] == "app.session_store"
    st, res, _ = rpc(server, "tools/call", {"name": "update_item", "arguments": {"item_id": item["id"], "selected": "redis", "comment": "可用性"}},
                     headers={"X-Reqviz-User": "agent-1"})
    assert res["result"]["structuredContent"]["selected"] == "redis"
    st, res, _ = rpc(server, "tools/call", {"name": "create_item", "arguments": {
        "system_id": sid, "layer": "os", "key": "memo", "name": "メモ", "kind": "document", "content_type": "json", "content": {"a": [1, 2]}}})
    assert res["result"]["structuredContent"]["content"] == {"a": [1, 2]}

    st, res, _ = rpc(server, "tools/call", {"name": "get_timeline", "arguments": {"system_id": sid, "entity": "item", "key": "app.session_store"}})
    tl = res["result"]["structuredContent"]
    assert [(r["version"], r["actor"], r["comment"]) for r in tl] == [(2, "agent-1", "可用性"), (1, "agent-1", "")]
    st, res, _ = rpc(server, "tools/call", {"name": "restore_revision", "arguments": {"revision_id": tl[1]["id"]}})
    assert res["result"]["structuredContent"]["result"]["selected"] == "sticky"
    st, res, _ = rpc(server, "tools/call", {"name": "diff_history", "arguments": {"system_id": sid, "from": f"rev:{tl[1]['id']}"}})
    assert [x["key"] for x in res["result"]["structuredContent"]["items"]] == ["os.memo"]

    st, res, _ = rpc(server, "tools/call", {"name": "export_system", "arguments": {"system_id": sid, "format": "csv"}})
    assert "参照名" in res["result"]["content"][0]["text"] and "structuredContent" not in res["result"]
    st, res, _ = rpc(server, "tools/call", {"name": "api_request", "arguments": {"method": "GET", "path": f"/api/systems/{sid}/layers"}})
    assert len(res["result"]["structuredContent"]) == 5


def test_tool_errors_are_results(server):
    sid = make_system(server)
    st, res, _ = rpc(server, "tools/call", {"name": "get_param", "arguments": {"param_id": 99999}})
    assert st == 200 and res["result"]["isError"] is True and "HTTP 404" in res["result"]["content"][0]["text"]
    st, res, _ = rpc(server, "tools/call", {"name": "create_param", "arguments": {"system_id": sid}})
    assert res["result"]["isError"] is True and "layer_id" in res["result"]["content"][0]["text"]
    st, res, _ = rpc(server, "tools/call", {"name": "list_params", "arguments": {"system_id": sid, "bogus": 1}})
    assert res["result"]["isError"] is True and "bogus" in res["result"]["content"][0]["text"]
    st, res, _ = rpc(server, "tools/call", {"name": "api_request", "arguments": {"method": "GET", "path": "/etc/passwd"}})
    assert res["result"]["isError"] is True
    # 存在しないツールはプロトコルエラー
    st, res, _ = rpc(server, "tools/call", {"name": "nope"})
    assert st == 400 and res["error"]["code"] == -32602


def test_modern_validation(server):
    st, res, _ = rpc(server, "tools/list", headers={"MCP-Protocol-Version": "2025-11-25"})
    assert st == 400 and res["error"]["code"] == -32020
    st, res, _ = rpc(server, "tools/list", headers={"MCP-Protocol-Version": None})
    assert st == 400 and res["error"]["code"] == -32020
    st, res, _ = rpc(server, "tools/list", headers={"Mcp-Method": "tools/call"})
    assert st == 400 and res["error"]["code"] == -32020
    st, res, _ = rpc(server, "tools/call", {"name": "list_systems"}, name="other")
    assert st == 400 and res["error"]["code"] == -32020 and "Mcp-Name" in res["error"]["message"]
    b64 = "=?base64?" + base64.b64encode(b"list_systems").decode() + "?="
    st, res, _ = rpc(server, "tools/call", {"name": "list_systems"}, name=b64)
    assert st == 200 and "result" in res

    body = {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {"_meta": {**META, "io.modelcontextprotocol/protocolVersion": "1900-01-01"}}}
    st, res, _ = http(server, "POST", "/mcp", body, {"MCP-Protocol-Version": "1900-01-01", "Mcp-Method": "tools/list"})
    assert st == 400 and res["id"] == 7 and res["error"]["code"] == -32022
    assert res["error"]["data"]["requested"] == "1900-01-01" and V in res["error"]["data"]["supported"]
    st, res, _ = rpc(server, "tools/list", meta=False)  # 新方式のヘッダーなのに _meta がない
    assert st == 400 and res["error"]["code"] == -32602

    st, res, _ = rpc(server, "resources/list")
    assert st == 404 and res["error"]["code"] == -32601
    st, res, _ = rpc(server, "ping")  # 新方式では削除されたメソッド
    assert st == 404 and res["error"]["code"] == -32601


def test_transport_rules(server):
    assert http(server, "GET", "/mcp")[0] == 405
    assert http(server, "DELETE", "/mcp")[0] == 405
    st, res, _ = http(server, "POST", "/mcp", b"{bad")
    assert st == 400 and res["error"]["code"] == -32700
    st, res, _ = http(server, "POST", "/mcp", [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}])
    assert st == 400 and res["error"]["code"] == -32600
    # DNS リバインディング対策: 他サイトの Origin は拒否、自分自身の Origin は許可
    st, res, _ = rpc(server, "tools/list", headers={"Origin": "http://evil.example"})
    assert st == 403
    assert rpc(server, "tools/list", headers={"Origin": server})[0] == 200


def test_legacy_handshake(server):
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}}
    st, res, hdrs = http(server, "POST", "/mcp", init)
    assert st == 200 and res["result"]["protocolVersion"] == "2025-06-18"
    assert res["result"]["capabilities"] == {"tools": {"listChanged": False}} and hdrs.get("Mcp-Session-Id") is None
    init["params"]["protocolVersion"] = "2099-01-01"
    assert http(server, "POST", "/mcp", init)[1]["result"]["protocolVersion"] == "2025-11-25"

    st, res, _ = http(server, "POST", "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert st == 202 and res is None
    h = {"MCP-Protocol-Version": "2025-06-18"}
    st, res, _ = http(server, "POST", "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, h)
    assert st == 200 and len(res["result"]["tools"]) == len(TOOLS) and "resultType" not in res["result"]
    st, res, _ = http(server, "POST", "/mcp", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_systems"}}, h)
    assert res["result"]["structuredContent"] == {"result": []}  # 旧方式ではオブジェクトに包む
    assert http(server, "POST", "/mcp", {"jsonrpc": "2.0", "id": 4, "method": "ping"}, h)[1]["result"] == {}
    st, res, _ = http(server, "POST", "/mcp", {"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, {"MCP-Protocol-Version": "2020-01-01"})
    assert st == 400 and res["error"]["code"] == -32022


def test_stdio_over_http_client(server):
    """stdio トランスポート（REST API を HTTP で呼ぶ構成）。"""
    sid = make_system(server)
    mcp = McpServer(HttpClient(server), "t")
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {"_meta": META}},
        {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 9}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_param_by_ref", "arguments": {"system_id": sid, "ref": "biz.peak_tps"}, "_meta": META}},
    ]
    out = io.StringIO()
    serve(mcp, io.StringIO("\n".join(json.dumps(x) for x in lines) + "\n\nnot json\n"), out, "cli-user")
    res = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r.get("id") for r in res] == [1, 2, None]
    assert res[0]["result"]["supportedVersions"][0] == V
    assert res[1]["result"]["structuredContent"]["value"] == 200
    assert res[2]["error"]["code"] == -32700

    down = call_tool(HttpClient("http://127.0.0.1:1"), "list_systems", {})
    assert down.status == 503 and "接続できません" in down.error_message
