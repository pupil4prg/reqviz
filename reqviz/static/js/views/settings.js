// 設定（操作者名・LLM 接続先）

import { api, currentUser, setCurrentUser } from "../api.js";
import { h, toast, fill } from "../ui.js";

export async function render(root) {
  const page = h("div", { class: "page" });
  root.append(page);
  const s = await api.get("/api/settings/llm");
  const origin = location.origin;
  const endpoint = (label, value, note) => h("tr", {}, h("td", {}, label), h("td", {}, h("code", {}, value)), h("td", { class: "small muted" }, note));

  const user = h("input", { value: currentUser(), placeholder: "例: 山田（未入力ならサーバーの既定ユーザー）" });
  const baseUrl = h("input", { value: s.base_url, placeholder: "例: http://llm-proxy.internal:4000/v1", style: { width: "100%" } });
  const model = h("input", { value: s.model, placeholder: "例: gpt-4o-mini / llama3.1:70b" });
  const timeout = h("input", { type: "number", min: 1, max: 600, value: s.timeout });

  fill(page, 
    h("h1", {}, "設定"),
    h("div", { class: "card" },
      h("h3", {}, "操作者"),
      h("p", { class: "small muted" }, "MVP（単一ユーザー運用）では、ここで入力した名前が履歴と監査ログに記録されます。名前はこのブラウザに保存されます。"),
      h("label", { class: "field" }, "名前", user),
      h("button", { class: "primary", onclick: () => { setCurrentUser(user.value.trim()); document.getElementById("user-badge").textContent = user.value.trim(); toast("保存しました"); } }, "保存")),
    h("div", { class: "card" },
      h("h3", {}, "LLM 接続先（OpenAI 互換 API）"),
      h("p", { class: "small muted" }, "LiteLLM Proxy・vLLM・Ollama など、/chat/completions を提供する閉域網内のエンドポイントを指定します。"),
      h("label", { class: "field" }, "ベース URL", baseUrl),
      h("div", { class: "grid2" }, h("label", { class: "field" }, "モデル", model), h("label", { class: "field" }, "タイムアウト（秒）", timeout)),
      h("p", { class: "small" }, "API キー: ", s.api_key_set ? h("span", { class: "badge approved" }, "設定済み（環境変数）") : h("span", { class: "badge" }, "未設定"),
        h("span", { class: "hint" }, " API キーは環境変数 REQVIZ_LLM_API_KEY で指定します（画面や DB には保存しません）。")),
      h("button", {
        class: "primary",
        onclick: async () => {
          try {
            await api.put("/api/settings/llm", { base_url: baseUrl.value.trim(), model: model.value.trim(), timeout: Number(timeout.value) });
            toast("保存しました");
          } catch (e) { toast(e.message, "error"); }
        },
      }, "保存")),
    h("div", { class: "card" },
      h("h3", {}, "外部連携（API・MCP・A2A）"),
      h("p", { class: "small muted" }, "画面でできる操作は、すべて REST API から行えます。AI エージェントからは MCP または A2A で接続します。",
        "これらの入口に認証はありません。信頼できるネットワーク内でのみ公開してください。"),
      h("table", { class: "tbl" },
        endpoint("REST API", `${origin}/api`, "GET でルートの一覧を返します。操作者名は X-Reqviz-User ヘッダーで指定"),
        endpoint("MCP（Streamable HTTP）", `${origin}/mcp`, "プロトコル 2026-07-28、および initialize を使う 2025-11-25 以前に対応"),
        endpoint("MCP（stdio）", "uv run --no-dev python -m reqviz.mcp", "--url を付けると、このサーバーの REST API を使います"),
        endpoint("A2A（JSON-RPC）", `${origin}/a2a`, "A2A 1.0。リクエストに A2A-Version: 1.0 が必要"),
        endpoint("A2A Agent Card", `${origin}/.well-known/agent-card.json`, "")),
      h("p", { class: "small" }, "A2A での文章による対話: ",
        s.enabled ? h("span", { class: "badge approved" }, "利用可（LLM 設定済み）") : h("span", { class: "badge" }, "LLM 未設定のため、ツールの直接指定のみ"))),
  );
}
