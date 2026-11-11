import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from reqviz.config import Config, LLMConfig
from reqviz.server import make_server

from ..conftest import ROOT


class FakeToolLLM:
    """ツール呼び出しに対応した OpenAI 互換の偽サーバー。replies に積んだ message を順に返す。"""

    def __init__(self):
        self.replies = []
        self.requests = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                fake.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                out = json.dumps({"choices": [{"message": fake.replies.pop(0)}]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def say(self, text):
        self.replies.append({"role": "assistant", "content": text})

    def call(self, *calls):
        self.replies.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": f"c{len(self.requests)}_{i}", "type": "function", "function": {"name": n, "arguments": json.dumps(a)}}
            for i, (n, a) in enumerate(calls)
        ]})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_llm():
    f = FakeToolLLM()
    yield f
    f.close()


def _serve(tmp_path, llm=None):
    cfg = Config(host="127.0.0.1", port=0, db_path=tmp_path / "agent.db", templates_dir=ROOT / "templates", auth_enabled=False,
                 llm=llm or LLMConfig())
    srv = make_server(cfg, run_ai_in_thread=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def server(tmp_path):
    srv = _serve(tmp_path)
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def llm_server(tmp_path, fake_llm):
    srv = _serve(tmp_path, LLMConfig(base_url=fake_llm.url, model="test-model", timeout=5))
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def http(base, method, path, body=None, headers=None):
    """(status, 解釈した JSON または None, ヘッダー) を返す。"""
    data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as res:
            raw, status, hdrs = res.read(), res.status, res.headers
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, e.headers
    return status, (json.loads(raw) if raw else None), hdrs


def make_system(base, name="販売管理"):
    tpl = http(base, "GET", "/api/templates")[1][0]
    return http(base, "POST", "/api/systems", {"name": name, "template_id": tpl["id"]})[1]["id"]
