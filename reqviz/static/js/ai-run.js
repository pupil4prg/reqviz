// AI ジョブの起動・ポーリング・結果表示（提案の採用／不採用）

import { api } from "./api.js";
import { fmtValue, h, KIND_LABELS, modal, showError, toast, fill } from "./ui.js";

export const PURPOSE_LABELS = {
  evaluate: "妥当性評価",
  suggest_values: "設計値の提案",
  suggest_rules: "ルール・関連の提案",
  rationale: "設計根拠の作成",
};

/** AI ジョブを起動し、完了したら結果ダイアログを表示する。onChange は提案採用などで呼ばれる。 */
export async function startAi(sid, purpose, targets, onChange) {
  let run;
  try {
    const { run_id } = await api.post(`/api/systems/${sid}/ai/${purpose}`, { targets });
    toast(`AI に「${PURPOSE_LABELS[purpose]}」を依頼しました…`);
    run = await poll(run_id);
  } catch (e) { showError(e); return; }
  if (run.status === "failed") {
    showError(new Error(`AI の処理に失敗しました: ${run.error}`));
  } else {
    toast(`AI の「${PURPOSE_LABELS[purpose]}」が完了しました`);
  }
  showRun(run, onChange);
  onChange && onChange();
}

async function poll(runId) {
  for (;;) {
    const run = await api.get(`/api/ai/runs/${runId}`);
    if (run.status === "done" || run.status === "failed") return run;
    await new Promise((r) => setTimeout(r, 1500));
  }
}

export function showRun(run, onChange) {
  const body = h("div");
  const render = (r) => fill(body, runBody(r, async (sug, adopt) => {
    try {
      await api.post(`/api/ai/suggestions/${sug.id}/${adopt ? "adopt" : "reject"}`);
      toast(adopt ? "提案を採用しました" : "提案を不採用にしました");
      render(await api.get(`/api/ai/runs/${r.id}`));
      onChange && onChange();
    } catch (e) { showError(e); }
  }));
  render(run);
  modal({ title: `AI: ${PURPOSE_LABELS[run.purpose]}（#${run.id}）`, body, wide: true });
}

export function runBody(run, decide) {
  const head = h("div", { class: "row small muted" },
    h("span", { class: `badge ${run.status}` }, run.status),
    `モデル: ${run.model || "-"}`, `対象: ${run.targets.length ? run.targets.join(", ") : "全パラメータ"}`);
  if (run.status === "failed") {
    return [head, h("div", { class: "alert error" }, run.error),
      run.response ? h("details", {}, h("summary", {}, "AI の応答（生データ）"), h("pre", { class: "mono small" }, run.response)) : null];
  }
  if (run.status !== "done") return [head, h("p", {}, "処理中です…")];
  const sugs = run.suggestions;
  if (!sugs.length) return [head, h("p", {}, "提案・指摘はありませんでした。")];

  const rows = sugs.map((s) => {
    const p = s.payload;
    let content;
    if (s.kind === "finding") {
      content = [h("span", { class: `badge ${p.severity === "info" ? "" : p.severity}` }, p.summary ? "所見" : p.severity), " ",
        s.target ? h("code", {}, s.target) : null, " ", p.message, p.rule ? h("span", { class: "muted small" }, `（${p.rule}）`) : null];
    } else if (s.kind === "value") {
      content = [h("code", {}, s.target), " = ", h("b", {}, fmtValue(p.value)), h("div", { class: "small muted" }, p.rationale)];
    } else if (s.kind === "rationale") {
      content = [h("code", {}, s.target), h("div", { class: "small" }, p.text)];
    } else if (s.kind === "rule") {
      content = [h("b", {}, p.key), ` [${KIND_LABELS[p.kind] || p.kind}] `, p.target ? h("code", {}, p.target + " = ") : null,
        h("code", {}, p.expression || p.nl_text || ""), h("div", { class: "small muted" }, p.description || "", " — ", p.reason || "")];
    } else if (s.kind === "relation") {
      content = [h("code", {}, p.from), " → ", h("code", {}, p.to), h("div", { class: "small muted" }, p.note || "")];
    }
    const actions = s.status === "pending"
      ? h("div", { class: "row" },
        h("button", { class: "small primary", onclick: () => decide(s, true) }, "採用"),
        h("button", { class: "small", onclick: () => decide(s, false) }, "不採用"))
      : s.status === "info" ? null : h("span", { class: `badge ${s.status === "adopted" ? "approved" : ""}` }, s.status === "adopted" ? "採用済み" : "不採用");
    return h("tr", {}, h("td", {}, content), h("td", { style: { width: "150px" } }, actions));
  });
  return [head, h("p", { class: "hint" }, "AI の提案は自動では反映されません。採用した値は「AI提案」として記録され、状態は下書きになります。"),
    h("table", { class: "tbl" }, rows)];
}
