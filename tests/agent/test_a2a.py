import json

from .conftest import http, make_system

H = {"A2A-Version": "1.0"}


def rpc(base, method, params=None, headers=None, rid=1):
    st, res, _ = http(base, "POST", "/a2a", {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}, {**H, **(headers or {})})
    assert st == 200 and res["jsonrpc"] == "2.0" and res["id"] == rid
    return res


_n = [0]


def send(base, text=None, data=None, task_id=None, context_id=None, metadata=None, config=None, headers=None):
    _n[0] += 1
    parts = ([{"text": text}] if text is not None else []) + ([{"data": data}] if data is not None else [])
    msg = {"messageId": f"m-{_n[0]}", "role": "ROLE_USER", "parts": parts}
    if task_id:
        msg["taskId"] = task_id
    if context_id:
        msg["contextId"] = context_id
    params = {"message": msg}
    if metadata:
        params["metadata"] = metadata
    if config:
        params["configuration"] = config
    res = rpc(base, "SendMessage", params, headers)
    return res["result"]["task"] if "result" in res else res["error"]


def text_of(task):
    return task["status"]["message"]["parts"][0]["text"]


def test_agent_card(server):
    st, card, _ = http(server, "GET", "/.well-known/agent-card.json")
    assert st == 200
    for f in ("name", "description", "supportedInterfaces", "version", "capabilities", "defaultInputModes", "defaultOutputModes", "skills"):
        assert f in card
    assert card["supportedInterfaces"] == [{"url": server + "/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}]
    assert card["capabilities"]["streaming"] is False
    assert all({"id", "name", "description", "tags"} <= set(s) for s in card["skills"])
    assert http(server, "POST", "/.well-known/agent-card.json", {})[0] == 405


def test_protocol_errors(server):
    st, res, _ = http(server, "POST", "/a2a", {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}})
    assert res["error"]["code"] == -32009  # A2A-Version なしは 0.3 とみなされ、非対応
    assert res["error"]["data"][0]["@type"].endswith("google.rpc.ErrorInfo") and res["error"]["data"][0]["reason"] == "VERSION_NOT_SUPPORTED"
    st, res, _ = http(server, "POST", "/a2a?A2A-Version=1.0", {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": "x"}})
    assert res["error"]["code"] == -32001  # クエリでの指定も可
    assert http(server, "POST", "/a2a", b"{bad", H)[1]["error"]["code"] == -32700
    assert http(server, "POST", "/a2a", {"id": 1}, H)[1]["error"]["code"] == -32600
    assert http(server, "GET", "/a2a")[0] == 405
    assert http(server, "POST", "/a2a", {}, {**H, "Origin": "http://evil.example"})[0] == 403

    assert rpc(server, "Nope")["error"]["code"] == -32601
    assert rpc(server, "SendStreamingMessage")["error"]["code"] == -32004
    assert rpc(server, "SubscribeToTask")["error"]["code"] == -32004
    assert rpc(server, "CreateTaskPushNotificationConfig")["error"]["code"] == -32003
    assert rpc(server, "GetExtendedAgentCard")["error"]["code"] == -32007
    assert rpc(server, "SendMessage", {})["error"]["code"] == -32602
    assert rpc(server, "SendMessage", {"message": {"messageId": "a", "role": "ROLE_AGENT", "parts": [{"text": "x"}]}})["error"]["code"] == -32602
    assert rpc(server, "SendMessage", {"message": {"messageId": "a", "role": "ROLE_USER", "parts": []}})["error"]["code"] == -32602
    assert rpc(server, "SendMessage", {"message": {"messageId": "a", "role": "ROLE_USER", "parts": [{"url": "http://x/y.png"}]}})["error"]["code"] == -32005
    assert send(server, "help", task_id="no-such-task")["code"] == -32001


def test_structured_commands_without_llm(server):
    sid = make_system(server)
    t = send(server, "help")
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and "list_params" in text_of(t)
    assert t["artifacts"][0]["parts"][0]["text"] == text_of(t) and t["contextId"]
    assert [m["role"] for m in t["history"]] == ["ROLE_USER", "ROLE_AGENT"]
    assert t["history"][0]["taskId"] == t["id"] and t["status"]["timestamp"].endswith("Z")

    # データパートでツールを直接指定（列挙データの作成）
    t = send(server, data={"tool": "create_item", "arguments": {
        "system_id": sid, "layer": "app", "key": "session_store", "name": "セッション管理", "kind": "choice",
        "options": ["sticky", "redis"], "selected": "sticky"}}, headers={"X-Reqviz-User": "planner-agent"})
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    art = next(a for a in t["artifacts"] if a["name"] == "create_item")
    assert art["parts"][0]["data"]["result"]["ref"] == "app.session_store" and art["parts"][0]["mediaType"] == "application/json"
    item_id = art["parts"][0]["data"]["result"]["id"]
    revs = http(server, "GET", f"/api/systems/{sid}/revisions?entity=item")[1]
    assert revs[0]["actor"] == "planner-agent"

    # テキストのコマンド形式
    t = send(server, f'list_items {{"system_id": {sid}, "kind": "choice"}}')
    assert next(a for a in t["artifacts"] if a["name"] == "list_items")["parts"][0]["data"]["result"][0]["selected"] == "sticky"
    t = send(server, f'update_item {{"item_id": {item_id}, "selected": "nope"}}')
    assert t["status"]["state"] == "TASK_STATE_FAILED" and "失敗" in text_of(t) and "artifacts" not in t

    # LLM なしで文章を送ると、形式を案内して入力待ちになる。同じタスクで続けられる
    t = send(server, "DB のコネクション数を教えて")
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and "LLM" in text_of(t)
    t2 = send(server, "list_systems", task_id=t["id"])
    assert t2["id"] == t["id"] and t2["status"]["state"] == "TASK_STATE_COMPLETED" and len(t2["history"]) == 4
    assert send(server, "help", task_id=t["id"])["code"] == -32004  # 完了したタスクには送れない
    t3 = send(server, "bad_json", task_id=None)
    assert t3["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert send(server, "help", task_id=t3["id"], context_id="other")["code"] == -32602


def test_task_operations(server):
    a = send(server, "help", context_id="ctx-A")
    b = send(server, "何か", context_id="ctx-A")
    c = send(server, "help")
    assert a["contextId"] == b["contextId"] == "ctx-A" != c["contextId"]

    got = rpc(server, "GetTask", {"id": a["id"], "historyLength": 1})["result"]
    assert got["id"] == a["id"] and len(got["history"]) == 1 and got["history"][0]["role"] == "ROLE_AGENT"
    assert "history" not in rpc(server, "GetTask", {"id": a["id"], "historyLength": 0})["result"]
    assert rpc(server, "GetTask", {"id": "missing"})["error"]["code"] == -32001

    lst = rpc(server, "ListTasks", {"contextId": "ctx-A"})["result"]
    assert lst["totalSize"] == 2 and lst["nextPageToken"] == "" and lst["pageSize"] == 50
    assert all("artifacts" not in t and "history" not in t for t in lst["tasks"])
    lst = rpc(server, "ListTasks", {"status": "TASK_STATE_COMPLETED", "pageSize": 1, "includeArtifacts": True})["result"]
    assert lst["totalSize"] == 2 and len(lst["tasks"]) == 1 and lst["nextPageToken"] == "1" and "artifacts" in lst["tasks"][0]
    assert len(rpc(server, "ListTasks", {"status": "TASK_STATE_COMPLETED", "pageSize": 1, "pageToken": "1"})["result"]["tasks"]) == 1
    assert rpc(server, "ListTasks", {"status": "done"})["error"]["code"] == -32602
    assert rpc(server, "ListTasks", {"pageSize": 0})["error"]["code"] == -32602

    canceled = rpc(server, "CancelTask", {"id": b["id"]})["result"]
    assert canceled["status"]["state"] == "TASK_STATE_CANCELED"
    assert rpc(server, "CancelTask", {"id": b["id"]})["error"]["code"] == -32002
    assert rpc(server, "CancelTask", {"id": a["id"]})["error"]["code"] == -32002


def test_llm_dialog_with_confirmation(llm_server, fake_llm):
    base = llm_server
    sid = make_system(base)
    peak = http(base, "GET", f"/api/systems/{sid}/params/by-ref/biz.peak_tps")[1]

    # 1) 参照だけの依頼は、確認なしで完了する
    fake_llm.call(("list_systems", {}))
    fake_llm.call(("get_param_by_ref", {"system_id": sid, "ref": "biz.peak_tps"}))
    fake_llm.say("ピーク時リクエスト数は 200 req/s です。")
    t = send(base, "販売管理のピーク TPS は？")
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and "200" in text_of(t)
    assert [a["name"] for a in t["artifacts"]] == ["response", "list_systems", "get_param_by_ref"]
    first = fake_llm.requests[0]
    assert first["messages"][0]["role"] == "system" and first["messages"][-1] == {"role": "user", "content": "販売管理のピーク TPS は？"}
    assert {x["function"]["name"] for x in first["tools"]} >= {"update_param", "create_item", "restore_revision"}
    assert fake_llm.requests[2]["messages"][-1]["role"] == "tool"
    ctx_id = t["contextId"]

    # 2) 変更を伴う依頼は、実行前に確認を求める（同じ文脈なので会話を引き継ぐ）
    fake_llm.call(("update_param", {"param_id": peak["id"], "value": 300, "comment": "需要予測の見直し"}),
                  ("recalc", {"system_id": sid, "mode": "stale"}))
    t = send(base, "300 に変更して再計算して", context_id=ctx_id)
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert "update_param" in text_of(t) and '"value": 300' in text_of(t)
    assert http(base, "GET", f"/api/params/{peak['id']}")[1]["value"] == 200  # まだ変わっていない
    assert any(m.get("content") == "販売管理のピーク TPS は？" for m in fake_llm.requests[3]["messages"])

    t = send(base, "はい", task_id=t["id"])  # 1 件目を承認 → 2 件目（recalc）の確認
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED" and "recalc" in text_of(t)
    assert http(base, "GET", f"/api/params/{peak['id']}")[1]["value"] == 300
    fake_llm.say("300 に変更し、再計算案を作成しました。")
    t = send(base, "yes", task_id=t["id"], headers={"X-Reqviz-User": "bob"})
    assert t["status"]["state"] == "TASK_STATE_COMPLETED" and "再計算案" in text_of(t)
    assert [a["name"] for a in t["artifacts"]] == ["update_param", "response", "recalc"]
    tool_msgs = [m for m in fake_llm.requests[-1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 4 and all(m["tool_call_id"] for m in tool_msgs)
    assert len(t["history"]) == 6

    # 3) 断った場合は実行しない
    fake_llm.call(("delete_param", {"param_id": peak["id"]}))
    t = send(base, "このパラメータを消して", context_id=ctx_id)
    assert t["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    fake_llm.say("削除は取りやめました。")
    t = send(base, "いいえ", task_id=t["id"])
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert http(base, "GET", f"/api/params/{peak['id']}")[0] == 200
    assert "承認しなかった" in fake_llm.requests[-1]["messages"][-1]["content"]

    # 4) autoApprove なら確認を省略する
    fake_llm.call(("update_param", {"param_id": peak["id"], "value": 350}))
    fake_llm.say("350 にしました。")
    t = send(base, "350 にして", metadata={"autoApprove": True})
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert http(base, "GET", f"/api/params/{peak['id']}")[1]["value"] == 350

    # 5) 確認待ちに別の指示を返すと、操作は取り消して新しい指示として扱う
    fake_llm.call(("update_param", {"param_id": peak["id"], "value": 1}))
    t = send(base, "1 にして")
    fake_llm.say("了解しました。変更していません。")
    t = send(base, "やっぱり現在値だけ教えて", task_id=t["id"])
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert fake_llm.requests[-1]["messages"][-1] == {"role": "user", "content": "やっぱり現在値だけ教えて"}
    assert http(base, "GET", f"/api/params/{peak['id']}")[1]["value"] == 350
    audit = http(base, "GET", f"/api/systems/{sid}/revisions?entity=param&key=biz.peak_tps")[1]
    assert [r["actor"] for r in audit][:2] == ["a2a", "a2a"]


def test_llm_failures(llm_server, fake_llm):
    # LLM が応答しない（replies が空で 500 相当）→ タスクは失敗
    fake_llm.replies.append(None)
    t = send(llm_server, "こんにちは")
    assert t["status"]["state"] == "TASK_STATE_FAILED" and "LLM" in text_of(t)
    # 不明なツールを呼んだ場合は、エラーを LLM に返して続行する
    fake_llm.call(("no_such_tool", {}))
    fake_llm.say("そのような操作はできません。")
    t = send(llm_server, "何かして")
    assert t["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "不明なツール" in fake_llm.requests[-1]["messages"][-1]["content"]
    # ツール呼び出しが止まらない場合は打ち切る
    for _ in range(8):
        fake_llm.call(("list_systems", {}))
    t = send(llm_server, "ループ")
    assert t["status"]["state"] == "TASK_STATE_FAILED" and "超えた" in text_of(t)


def test_return_immediately(tmp_path):
    """returnImmediately = true なら処理中のタスクを返し、結果は GetTask で取得する。"""
    import threading
    import time

    from reqviz.config import Config, LLMConfig
    from reqviz.server import make_server

    from ..conftest import ROOT

    srv = make_server(Config(host="127.0.0.1", port=0, db_path=tmp_path / "t.db", templates_dir=ROOT / "templates", llm=LLMConfig()))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        t = send(base, "list_systems", config={"returnImmediately": True, "historyLength": 0})
        assert t["status"]["state"] in ("TASK_STATE_WORKING", "TASK_STATE_COMPLETED") and "history" not in t
        for _ in range(50):
            got = rpc(base, "GetTask", {"id": t["id"]})["result"]
            if got["status"]["state"] == "TASK_STATE_COMPLETED":
                break
            time.sleep(0.1)
        assert got["status"]["state"] == "TASK_STATE_COMPLETED" and got["artifacts"][1]["name"] == "list_systems"
    finally:
        srv.shutdown()
        srv.server_close()
