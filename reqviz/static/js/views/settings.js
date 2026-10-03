// 設定（操作者名・LLM 接続先）

import { api, currentUser, setCurrentUser } from "../api.js";
import { h, toast, fill } from "../ui.js";

export async function render(root) {
  const page = h("div", { class: "page" });
  root.append(page);
  const s = await api.get("/api/settings/llm");

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
  );
}
