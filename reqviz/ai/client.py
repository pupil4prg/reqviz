"""OpenAI 互換 Chat Completions API クライアント（urllib のみ）。

LiteLLM Proxy / vLLM / Ollama など、``POST {base_url}/chat/completions`` を
提供するエンドポイントで動作する。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from ..config import LLMConfig


class LLMError(Exception):
    pass


Opener = Callable[..., Any]


def _post(cfg: LLMConfig, body: dict[str, Any], opener: Opener) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    req = urllib.request.Request(
        f"{cfg.base_url}/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with opener(req, timeout=cfg.timeout) as res:
        return json.loads(res.read().decode("utf-8"))


def chat(
    cfg: LLMConfig,
    messages: list[dict[str, str]],
    json_mode: bool = True,
    opener: Opener = urllib.request.urlopen,
    retries: int = 1,
) -> tuple[str, dict[str, Any]]:
    """メッセージを送り、(応答本文, 生レスポンス) を返す。"""
    if not cfg.enabled:
        raise LLMError("LLM の接続先が設定されていません（REQVIZ_LLM_BASE_URL / REQVIZ_LLM_MODEL）")
    body: dict[str, Any] = {"model": cfg.model, "messages": messages, "temperature": 0.2}
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            raw = _post(cfg, body, opener)
            content = raw["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise LLMError("応答の形式が不正です")
            return content, raw
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500] if e.fp else ""
            # response_format 非対応のモデルは指定を外して再試行
            if e.code == 400 and "response_format" in body:
                body.pop("response_format")
                last = LLMError(f"HTTP {e.code}: {detail}")
                continue
            last = LLMError(f"HTTP {e.code}: {detail}")
            if e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = LLMError(f"接続できません: {e}")
        except (KeyError, IndexError, ValueError) as e:
            last = LLMError(f"応答の形式が不正です: {e}")
    raise last or LLMError("不明なエラー")


def chat_tools(
    cfg: LLMConfig,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    opener: Opener = urllib.request.urlopen,
) -> dict[str, Any]:
    """ツール呼び出し（function calling）つきで送り、応答の message オブジェクトを返す。

    返り値は ``{"role": "assistant", "content": str | None, "tool_calls": [...]}`` の形（OpenAI 互換）。
    """
    if not cfg.enabled:
        raise LLMError("LLM の接続先が設定されていません（REQVIZ_LLM_BASE_URL / REQVIZ_LLM_MODEL）")
    body: dict[str, Any] = {"model": cfg.model, "messages": messages, "temperature": 0.2}
    if tools:
        body["tools"] = tools
    try:
        raw = _post(cfg, body, opener)
        message = raw["choices"][0]["message"]
        if not isinstance(message, dict):
            raise LLMError("応答の形式が不正です")
        return message
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500] if e.fp else ""
        raise LLMError(f"HTTP {e.code}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"接続できません: {e}") from None
    except (KeyError, IndexError, ValueError) as e:
        raise LLMError(f"応答の形式が不正です: {e}") from None


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def parse_json(content: str) -> Any:
    """応答本文から JSON を取り出す（コードフェンスや前後の文章を許容）。"""
    text = content.strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            pass
    raise LLMError("応答が JSON ではありません")
