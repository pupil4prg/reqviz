import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from reqviz.ai import client, jobs, schema
from reqviz.config import LLMConfig
from reqviz.errors import Invalid
from reqviz.repo import model as repo


class FakeLLM:
    """OpenAI 互換の偽サーバー。responses に積んだ (status, content) を順に返す。"""

    def __init__(self):
        self.responses = []
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
                status, content = fake.responses.pop(0)
                if status == 200:
                    out = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
                else:
                    out = content.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeLLM()
    yield f
    f.close()


def cfg(fake, **kw):
    return LLMConfig(base_url=fake.url, model="test-model", api_key=kw.get("api_key", "sk-test"), timeout=5)


# ------------------------------------------------------------ client

def test_chat_ok_with_auth(fake):
    fake.responses.append((200, '{"a": 1}'))
    content, _ = client.chat(cfg(fake), [{"role": "user", "content": "hi"}])
    assert content == '{"a": 1}'
    req = fake.requests[0]
    assert req["path"] == "/v1/chat/completions" and req["auth"] == "Bearer sk-test"
    assert req["body"]["response_format"] == {"type": "json_object"}


def test_chat_retries_without_response_format(fake):
    fake.responses += [(400, "response_format not supported"), (200, "{}")]
    client.chat(cfg(fake), [])
    assert "response_format" not in fake.requests[1]["body"]


def test_chat_server_error_retry_then_fail(fake):
    fake.responses += [(500, "boom"), (500, "boom")]
    with pytest.raises(client.LLMError, match="HTTP 500"):
        client.chat(cfg(fake), [])
    assert len(fake.requests) == 2


def test_chat_not_configured():
    with pytest.raises(client.LLMError, match="設定"):
        client.chat(LLMConfig(), [])


def test_chat_connection_error():
    c = LLMConfig(base_url="http://127.0.0.1:1/v1", model="m", timeout=1)
    with pytest.raises(client.LLMError, match="接続"):
        client.chat(c, [], retries=0)


@pytest.mark.parametrize(
    "content, expected",
    [
        ('{"x": 1}', {"x": 1}),
        ('```json\n{"x": 2}\n```', {"x": 2}),
        ('結果です: {"x": 3} 以上', {"x": 3}),
    ],
)
def test_parse_json(content, expected):
    assert client.parse_json(content) == expected


def test_parse_json_fail():
    with pytest.raises(client.LLMError):
        client.parse_json("JSON ではない")


def test_schema():
    s = {"type": "object", "required": ["a"], "properties": {
        "a": {"type": "array", "items": {"type": ["number", "null"]}},
        "b": {"type": "string", "enum": ["x"]}}}
    assert schema.validate({"a": [1, None]}, s) == []
    errs = schema.validate({"a": [True, "s"], "b": "y"}, s)
    assert len(errs) == 3
    assert schema.validate([], s) == ["$: object が必要です"]
    assert schema.validate({}, s) == ["$.a: 必須です"]


# ------------------------------------------------------------ jobs

def run(ctx, sid, fake, purpose, content, targets=()):
    fake.responses.append((200, content))
    run_id = jobs.create_run(ctx, sid, purpose, list(targets), "test-model")
    return jobs.execute_run(ctx, run_id, cfg(fake))


def test_evaluate_marks_ai_evaluated(ctx, web3, fake):
    content = json.dumps({"summary": "概ね妥当", "findings": [
        {"param": "db.pool_per_server", "severity": "warning", "message": "多すぎる", "rule": "N-001"},
        {"param": "unknown.x", "severity": "info", "message": "?", "rule": None}]})
    r = run(ctx, web3, fake, "evaluate", content, ["db.pool_per_server", "biz.peak_tps"])
    assert r["status"] == "done"
    assert [s["status"] for s in r["suggestions"]] == ["info", "info", "info"]
    assert r["suggestions"][2]["target"] is None
    params = repo.params_by_ref(ctx, web3)
    assert params["db.pool_per_server"]["status"] == "ai_evaluated"
    assert params["biz.business_type"]["status"] == "draft"
    sent = fake.requests[0]["body"]["messages"][1]["content"]
    assert "N-001" in sent and "db.pool_per_server" in sent


def test_suggest_values_adopt(ctx, web3, fake):
    content = json.dumps({"suggestions": [
        {"param": "app.server_count", "value": 4, "rationale": "N+1 構成"},
        {"param": "nope.x", "value": 1, "rationale": "-"}]})
    r = run(ctx, web3, fake, "suggest_values", content)
    assert len(r["suggestions"]) == 1
    sug = r["suggestions"][0]
    out = jobs.decide_suggestion(ctx, sug["id"], True)
    assert out["status"] == "adopted" and out["decided_by"] == "tester"
    p = repo.params_by_ref(ctx, web3)["app.server_count"]
    assert p["value"] == 4 and p["source"] == "ai"
    assert p["rationale"] == "冗長構成（N+1）"  # 既存の根拠は上書きしない
    assert repo.params_by_ref(ctx, web3)["app.threads_per_server"]["flag_stale"]
    with pytest.raises(Invalid):
        jobs.decide_suggestion(ctx, sug["id"], True)


def test_suggest_rules_adopt_and_reject(ctx, web3, fake):
    content = json.dumps({
        "rules": [
            {"key": "C-AI1", "kind": "constraint", "target": None, "expression": "app.server_count >= 2",
             "nl_text": None, "severity": "warning", "description": "冗長化", "reason": "可用性"},
            {"key": "C-AI2", "kind": "constraint", "expression": "zzz.q > 1", "severity": "error", "reason": "x"}],
        "relations": [{"from": "biz.peak_tps", "to": "app.server_count", "note": "流量と台数"}]})
    r = run(ctx, web3, fake, "suggest_rules", content)
    s1, s2, s3 = r["suggestions"]
    jobs.decide_suggestion(ctx, s1["id"], True)
    assert any(x["key"] == "C-AI1" for x in repo.list_rules(ctx, web3))
    with pytest.raises(Invalid):
        jobs.decide_suggestion(ctx, s2["id"], True)  # 不正な式は採用できない
    assert jobs.decide_suggestion(ctx, s2["id"], False)["status"] == "rejected"
    jobs.decide_suggestion(ctx, s3["id"], True)
    assert len(repo.list_relations(ctx, web3)) == 2


def test_rationale_adopt(ctx, web3, fake):
    content = json.dumps({"rationales": [{"param": "app.concurrent_requests", "text": "リトルの法則より算出。"}]})
    r = run(ctx, web3, fake, "rationale", content)
    jobs.decide_suggestion(ctx, r["suggestions"][0]["id"], True)
    assert repo.params_by_ref(ctx, web3)["app.concurrent_requests"]["rationale"] == "リトルの法則より算出。"


def test_invalid_response_marks_failed(ctx, web3, fake):
    r = run(ctx, web3, fake, "evaluate", json.dumps({"findings": [{"severity": "bad"}]}))
    assert r["status"] == "failed" and "形式" in r["error"]
    assert r["response"]  # 生の応答は保存される
    r = run(ctx, web3, fake, "evaluate", "not json")
    assert r["status"] == "failed"
    assert repo.params_by_ref(ctx, web3)["biz.peak_tps"]["status"] == "draft"


def test_create_run_validation(ctx, web3):
    with pytest.raises(Invalid):
        jobs.create_run(ctx, web3, "fly", [], "m")
    with pytest.raises(Invalid):
        jobs.create_run(ctx, web3, "evaluate", ["no.such"], "m")


def test_settings_override(ctx):
    base = LLMConfig(base_url="http://env/v1", model="env-model", api_key="k")
    assert jobs.get_settings(ctx, base)["model"] == "env-model"
    s = jobs.put_settings(ctx, base, {"model": "db-model", "timeout": 9999})
    assert s["model"] == "db-model" and s["timeout"] == 600 and s["api_key_set"]
    with pytest.raises(Invalid):
        jobs.put_settings(ctx, base, {"timeout": "abc"})
