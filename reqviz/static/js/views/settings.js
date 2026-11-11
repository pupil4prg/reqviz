// 設定（操作者名・LLM 接続先）

import { api, currentUser, setCurrentUser } from "../api.js";
import { app, refreshAuth } from "../main.js";
import { confirmDialog, fmtTime, h, modal, showError, toast, fill } from "../ui.js";

export async function render(root) {
  const page = h("div", { class: "page" });
  root.append(page);
  const s = await api.get("/api/settings/llm");
  const origin = location.origin;
  const endpoint = (label, value, note) => h("tr", {}, h("td", {}, label), h("td", {}, h("code", {}, value)), h("td", { class: "small muted" }, note));

  const authOn = app.auth.auth_enabled;
  const me = app.auth.user;
  const isAdmin = !authOn || me.role === "admin";
  const user = h("input", { value: currentUser(), placeholder: "例: 山田（未入力ならサーバーの既定ユーザー）" });
  const tokensBox = h("div");

  async function loadTokens() {
    const tokens = await api.get("/api/auth/tokens");
    fill(tokensBox, tokens.length
      ? h("table", { class: "tbl" },
        h("tr", {}, ["名前", "トークン", "発行日時", "最後に使った日時", ""].map((x) => h("th", {}, x))),
        tokens.map((t) => h("tr", {},
          h("td", {}, t.name), h("td", {}, h("code", {}, `${t.prefix}…`)),
          h("td", { class: "small" }, fmtTime(t.created_at)), h("td", { class: "small" }, fmtTime(t.last_used_at) || "未使用"),
          h("td", {}, h("button", { class: "small danger", onclick: () => revoke(t) }, "失効")))))
      : h("p", { class: "muted small" }, "発行済みのトークンはありません"));
  }

  async function issue(nameEl) {
    try {
      const t = await api.post("/api/auth/tokens", { name: nameEl.value });
      nameEl.value = "";
      modal({
        title: "API トークンを発行しました",
        body: [h("p", {}, "このトークンは今だけ表示されます。控えてから閉じてください。"), h("div", { class: "token-once" }, t.token),
          h("p", { class: "hint" }, "使い方: HTTP ヘッダーに Authorization: Bearer <トークン> を付けます。stdio の MCP では --token または環境変数 REQVIZ_API_TOKEN で指定します。")],
      });
      loadTokens();
    } catch (e) { showError(e); }
  }

  async function revoke(t) {
    if (!(await confirmDialog("トークンを失効", `「${t.name}」を失効させます。このトークンを使っている連携は動かなくなります。`, "失効", true))) return;
    try { await api.del(`/api/auth/tokens/${t.id}`); loadTokens(); } catch (e) { showError(e); }
  }

  function accountCards() {
    const cur = h("input", { type: "password", autocomplete: "current-password" });
    const next = h("input", { type: "password", autocomplete: "new-password" });
    const again = h("input", { type: "password", autocomplete: "new-password" });
    const tokenName = h("input", { placeholder: "用途（例: MCP クライアント）", style: { width: "280px" } });
    return [
      h("div", { class: "card" },
        h("h3", {}, "アカウント"),
        h("p", {}, h("b", {}, me.display_name), " ", h("code", {}, me.login), " ", h("span", { class: "badge" }, app.auth.roles[me.role] || me.role)),
        h("p", { class: "small muted" }, "履歴と監査ログには、この ID が操作者として記録されます。表示名とロールの変更は管理者に依頼してください。"),
        h("div", { class: "grid2" }, h("label", { class: "field" }, "現在のパスワード", cur), h("span")),
        h("div", { class: "grid2" }, h("label", { class: "field" }, "新しいパスワード（8 文字以上）", next), h("label", { class: "field" }, "新しいパスワード（確認）", again)),
        h("button", {
          class: "primary",
          onclick: async () => {
            if (next.value !== again.value) return toast("新しいパスワードが確認用と一致しません", "error");
            try {
              await api.post("/api/auth/password", { current_password: cur.value, new_password: next.value });
              cur.value = next.value = again.value = "";
              toast("パスワードを変更しました（他の端末はログアウトされます）");
              refreshAuth();
            } catch (e) { showError(e); }
          },
        }, "パスワードを変更")),
      h("div", { class: "card" },
        h("h3", {}, "API トークン"),
        h("p", { class: "small muted" }, "REST API・MCP・A2A をプログラムやエージェントから使うためのトークンです。トークンでの操作は、あなたの ID とロールで行われます。"),
        tokensBox,
        h("div", { class: "row", style: { marginTop: "8px" } }, tokenName, h("button", { onclick: () => issue(tokenName) }, "トークンを発行"))),
    ];
  }

  const baseUrl = h("input", { value: s.base_url, placeholder: "例: http://llm-proxy.internal:4000/v1", style: { width: "100%" } });
  const model = h("input", { value: s.model, placeholder: "例: gpt-4o-mini / llama3.1:70b" });
  const timeout = h("input", { type: "number", min: 1, max: 600, value: s.timeout });

  fill(page, 
    h("h1", {}, "設定"),
    authOn ? accountCards() : h("div", { class: "card" },
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
      isAdmin ? null : h("p", { class: "hint" }, "接続先を変更できるのは管理者だけです。"),
      h("button", {
        class: "primary", disabled: !isAdmin,
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
        authOn ? "プログラムから使う場合は、上の API トークンを Authorization: Bearer ヘッダーで渡します。"
          : "ログイン認証が無効（REQVIZ_AUTH=off）のため、これらの入口に認証はありません。信頼できるネットワーク内でのみ公開してください。"),
      h("table", { class: "tbl" },
        endpoint("REST API", `${origin}/api`, authOn ? "GET でルートの一覧を返します" : "GET でルートの一覧を返します。操作者名は X-Reqviz-User ヘッダーで指定"),
        endpoint("MCP（Streamable HTTP）", `${origin}/mcp`, "プロトコル 2026-07-28、および initialize を使う 2025-11-25 以前に対応"),
        endpoint("MCP（stdio）", "uv run --no-dev python -m reqviz.mcp", "--url を付けると、このサーバーの REST API を使います"),
        endpoint("A2A（JSON-RPC）", `${origin}/a2a`, "A2A 1.0。リクエストに A2A-Version: 1.0 が必要"),
        endpoint("A2A Agent Card", `${origin}/.well-known/agent-card.json`, "")),
      h("p", { class: "small" }, "A2A での文章による対話: ",
        s.enabled ? h("span", { class: "badge approved" }, "利用可（LLM 設定済み）") : h("span", { class: "badge" }, "LLM 未設定のため、ツールの直接指定のみ"))),
  );
  if (authOn) loadTokens().catch(showError);
}
