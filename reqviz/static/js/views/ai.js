// AI 実行履歴と、新しい AI 処理の依頼

import { api } from "../api.js";
import { PURPOSE_LABELS, showRun, startAi } from "../ai-run.js";
import { fmtTime, h, showError, fill } from "../ui.js";

export async function render(root, { sid }) {
  const page = h("div", { class: "page" });
  root.append(page);

  async function load() {
    const [runs, settings, params] = await Promise.all([
      api.get(`/api/systems/${sid}/ai/runs`), api.get("/api/settings/llm"), api.get(`/api/systems/${sid}/params`),
    ]);
    const purpose = h("select", {}, Object.entries(PURPOSE_LABELS).map(([k, v]) => h("option", { value: k }, v)));
    const targets = h("select", { multiple: true, size: 8, style: { minWidth: "320px" } },
      params.map((p) => h("option", { value: p.ref }, `${p.ref}（${p.name}）`)));
    fill(page, 
      h("h1", {}, "AI 支援"),
      settings.enabled
        ? h("p", { class: "small muted" }, `接続先: ${settings.base_url} ／ モデル: ${settings.model}`)
        : h("div", { class: "alert warn" }, "LLM の接続先が未設定です。", h("a", { href: "#/settings" }, "設定画面"), "で接続先とモデルを指定してください。式による計算と制約チェックは AI なしでも利用できます。"),
      h("div", { class: "card" },
        h("h3", {}, "AI に依頼する"),
        h("div", { class: "row", style: { alignItems: "flex-start" } },
          h("label", { class: "field" }, "処理", purpose),
          h("label", { class: "field" }, "対象パラメータ（未選択なら全体。Ctrl / Shift で複数選択）", targets)),
        h("button", {
          class: "primary", disabled: !settings.enabled,
          onclick: () => startAi(sid, purpose.value, [...targets.selectedOptions].map((o) => o.value), load),
        }, "実行"),
        h("p", { class: "hint" }, "妥当性評価が完了すると、対象の下書きパラメータは「AI評価済み」になります。提案は採用するまで反映されません。")),
      h("h2", {}, "実行履歴"),
      runs.length
        ? h("table", { class: "tbl" },
          h("tr", {}, ["#", "処理", "対象", "状態", "依頼者", "日時", ""].map((x) => h("th", {}, x))),
          runs.map((r) => h("tr", {},
            h("td", {}, r.id), h("td", {}, PURPOSE_LABELS[r.purpose]),
            h("td", { class: "small mono" }, r.targets.length ? r.targets.join(", ") : "全体"),
            h("td", {}, h("span", { class: `badge ${r.status}` }, r.status), r.error ? h("div", { class: "err small" }, r.error) : null),
            h("td", { class: "small" }, r.actor), h("td", { class: "small" }, fmtTime(r.created_at)),
            h("td", {}, h("button", { class: "small", onclick: async () => { try { showRun(await api.get(`/api/ai/runs/${r.id}`), load); } catch (e) { showError(e); } } }, "結果")))))
        : h("p", { class: "muted" }, "まだ実行されていません"),
    );
  }

  await load();
}
