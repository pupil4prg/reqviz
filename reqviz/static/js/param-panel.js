// パラメータ詳細パネル（値の編集・状態遷移・関連ルール・履歴）

import { api } from "./api.js";
import { startAi } from "./ai-run.js";
import {
  confirmDialog, flagBadges, fmtTime, fmtValue, h, KIND_LABELS, promptComment, readValue, showError,
  SOURCE_LABELS, statusBadge, STATUS_LABELS, toast, TYPE_LABELS, valueInput,
  fill,
} from "./ui.js";

const ACTION_LABELS = {
  "param.skip_ai": "AI評価をスキップ", "param.ai_evaluate": "AI評価済み", "param.request_review": "レビュー依頼",
  "param.approve": "承認", "param.reject": "差し戻し", "param.withdraw": "下書きに戻す", "param.ack_upstream": "上流変更を確認",
  "param.revert_to_draft": "値の変更により下書きへ",
};

/**
 * @param {HTMLElement} container
 * @param {number} paramId
 * @param {{sid:number, graph:object, onChange:Function, onTrace:Function}} opts
 */
export async function renderParamPanel(container, paramId, { sid, graph, onChange, onTrace }) {
  const p = await api.get(`/api/params/${paramId}`);
  const node = graph ? graph.nodes.find((n) => n.id === paramId) || {} : {};
  const rules = graph ? relatedRules(graph, p.ref) : [];

  const valEl = valueInput(p, p.value);
  const unitEl = h("input", { value: p.unit });
  const rationaleEl = h("textarea", { placeholder: "算出根拠・設計判断の理由" }, p.rationale);
  const refDocEl = h("input", { value: p.ref_doc, placeholder: "例: 非機能要件定義書 3.2" });
  const ownerEl = h("input", { value: p.owner });
  const commentEl = h("input", { placeholder: "変更理由（履歴に残ります・任意）" });

  async function save() {
    try {
      const body = {
        value: readValue(p, valEl), unit: unitEl.value, rationale: rationaleEl.value, ref_doc: refDocEl.value,
        owner: ownerEl.value, row_version: p.row_version, comment: commentEl.value,
      };
      const after = await api.put(`/api/params/${p.id}`, body);
      toast(after.status !== p.status ? `保存しました（値の変更により${STATUS_LABELS[after.status]}に戻りました）` : "保存しました");
      onChange();
    } catch (e) { showError(e); }
  }

  async function transition(action, needComment, label) {
    let comment = "";
    if (needComment) {
      comment = await promptComment({ title: label, required: true, okLabel: label });
      if (comment === null) return;
    }
    try {
      await api.post(`/api/params/${p.id}/transition`, { action, comment, row_version: p.row_version });
      toast(`${label}しました`);
      onChange();
    } catch (e) { showError(e); }
  }

  async function applyProposal() {
    try {
      await api.post(`/api/systems/${sid}/apply-proposals`, { refs: [p.ref] });
      toast(p.status !== "draft" ? "再計算案を適用しました（下書きに戻りました）" : "再計算案を適用しました");
      onChange();
    } catch (e) { showError(e); }
  }

  async function recalcThis() {
    try {
      const res = await api.post(`/api/systems/${sid}/recalc`, { mode: "downstream", refs: [p.ref] });
      const mine = res.find((r) => r.ref === p.ref);
      toast(mine ? (mine.status === "unchanged" ? "値は変わりませんでした" : mine.status === "error" ? `再計算エラー: ${mine.error}` : "再計算案を作成しました") : "再計算の対象はありません");
      onChange();
    } catch (e) { showError(e); }
  }

  async function remove() {
    if (!(await confirmDialog("パラメータを削除", `${p.ref} を削除します。`, "削除", true))) return;
    try { await api.del(`/api/params/${p.id}`); toast("削除しました"); onChange(null); } catch (e) { showError(e); }
  }

  const wf = workflowButtons(p, transition, () => startAi(sid, "evaluate", [p.ref], onChange));

  fill(container, 
    h("h2", {}, p.name),
    h("div", { class: "row" }, h("code", {}, p.ref), statusBadge(p.status), flagBadges({ ...p, violation: node.violation })),
    h("dl", { class: "kv" },
      h("dt", {}, "型"), h("dd", {}, TYPE_LABELS[p.type], p.type === "enum" ? `（${p.enum_options.join(" / ")}）` : ""),
      h("dt", {}, "値の出所"), h("dd", {}, SOURCE_LABELS[p.source]),
      h("dt", {}, "更新"), h("dd", {}, fmtTime(p.updated_at))),

    p.has_proposal || p.proposed_error
      ? h("div", { class: `alert ${p.proposed_error ? "error" : "warn"}` },
        p.proposed_error
          ? ["再計算エラー: ", p.proposed_error]
          : [h("div", {}, "再計算案: ", h("b", {}, fmtValue(p.value) || "（未設定）"), " → ", h("b", {}, fmtValue(p.proposed_value))),
            h("div", { class: "row", style: { marginTop: "6px" } },
              h("button", { class: "small primary", onclick: applyProposal }, "この案を適用"),
              p.status !== "draft" ? h("span", { class: "hint" }, "適用すると下書きに戻ります") : null)])
      : null,
    p.flag_upstream_changed
      ? h("div", { class: "alert warn" }, "承認後に上流の値が変更されています。影響を確認してください。") : null,

    node.formula
      ? h("section", {}, h("h3", {}, "算出式 ", h("span", { class: "muted small" }, node.formula_rule)),
        h("div", { class: "formula-box" }, `${p.ref} = ${node.formula}`),
        h("div", { class: "row", style: { marginTop: "6px" } },
          h("button", { class: "small", onclick: recalcThis }, "このパラメータを再計算")))
      : null,

    h("section", {},
      h("h3", {}, "値"),
      h("div", { class: "grid2" }, h("label", { class: "field" }, "値", valEl), h("label", { class: "field" }, "単位", unitEl)),
      h("label", { class: "field" }, "根拠", rationaleEl),
      h("div", { class: "grid2" }, h("label", { class: "field" }, "出典", refDocEl), h("label", { class: "field" }, "担当", ownerEl)),
      h("label", { class: "field" }, "変更理由", commentEl),
      h("div", { class: "row" }, h("button", { class: "primary", onclick: save }, "保存"),
        p.status !== "draft" ? h("span", { class: "hint" }, "値を変えると下書きに戻ります") : null)),

    h("section", {},
      h("h3", {}, "承認フロー"),
      p.review_blockers.length && p.status === "ai_evaluated"
        ? h("div", { class: "alert warn small" }, "レビュー依頼できません", h("ul", {}, p.review_blockers.map((r) => h("li", {}, r)))) : null,
      h("div", { class: "row" }, wf)),

    h("section", {},
      h("h3", {}, "AI 支援"),
      h("div", { class: "row" },
        h("button", { class: "small", onclick: () => startAi(sid, "evaluate", [p.ref], onChange) }, "妥当性を評価"),
        !node.formula ? h("button", { class: "small", onclick: () => startAi(sid, "suggest_values", [p.ref], onChange) }, "値を提案") : null,
        h("button", { class: "small", onclick: () => startAi(sid, "rationale", [p.ref], onChange) }, "根拠を作成"))),

    h("section", {},
      h("h3", {}, "影響範囲"),
      h("div", { class: "row" },
        h("button", { class: "small", onclick: () => onTrace(p.ref, "up") }, "← 上流"),
        h("button", { class: "small", onclick: () => onTrace(p.ref, "both") }, "両方"),
        h("button", { class: "small", onclick: () => onTrace(p.ref, "down") }, "下流 →"),
        h("button", { class: "small link", onclick: () => onTrace(null) }, "解除"))),

    rules.length
      ? h("section", {}, h("h3", {}, "関連するルール"),
        h("ul", { class: "timeline" }, rules.map((r) => h("li", {},
          h("b", {}, r.key), ` [${KIND_LABELS[r.kind] || "制約"}] `,
          r.violation ? h("span", { class: `badge ${r.severity}` }, "違反") : null,
          h("div", { class: "mono small" }, r.expression || r.nl_text || ""),
          r.description ? h("div", { class: "small muted" }, r.description) : null))))
      : null,

    h("section", {}, h("h3", {}, "履歴"), historyList(p)),
    h("section", {}, h("button", { class: "small danger", onclick: remove }, "このパラメータを削除")),
  );
}

function workflowButtons(p, transition, aiEvaluate) {
  switch (p.status) {
    case "draft":
      return [
        h("button", { class: "small primary", onclick: aiEvaluate }, "AI評価"),
        h("button", { class: "small", onclick: () => transition("skip_ai", false, "AI評価をスキップ") }, "AI評価をスキップ"),
      ];
    case "ai_evaluated":
      return [
        h("button", { class: "small primary", onclick: () => transition("request_review", false, "レビュー依頼") }, "レビュー依頼"),
        h("button", { class: "small", onclick: () => transition("withdraw", false, "下書きに戻") }, "下書きに戻す"),
      ];
    case "in_review":
      return [
        h("button", { class: "small primary", onclick: () => transition("approve", false, "承認") }, "承認"),
        h("button", { class: "small danger", onclick: () => transition("reject", true, "差し戻し") }, "差し戻し"),
        h("button", { class: "small", onclick: () => transition("withdraw", false, "下書きに戻") }, "取り下げ"),
      ];
    case "approved":
      return p.flag_upstream_changed
        ? [h("button", { class: "small primary", onclick: () => transition("ack_upstream", true, "上流変更を確認済みに") }, "上流変更を確認済みにする")]
        : [h("span", { class: "muted small" }, "承認済みです。値を変更すると下書きに戻ります。")];
    default:
      return [];
  }
}

function relatedRules(graph, ref) {
  const out = [];
  for (const r of graph.rule_nodes) {
    if (graph.edges.some((e) => e.to === r.id && e.from === ref)) out.push({ ...r, kind: "constraint" });
  }
  const node = graph.nodes.find((n) => n.ref === ref);
  for (const key of (node && node.nl_rules) || []) out.push({ key, kind: "nl", expression: "（自然言語ルール）" });
  return out;
}

function historyList(p) {
  const items = [
    ...p.revisions.map((r) => ({
      at: r.at, actor: r.actor,
      text: r.op === "create" ? "作成" : r.op === "delete" ? "削除" : r.changes.map((c) => `${c.field}: ${fmtValue(c.before) || "∅"} → ${fmtValue(c.after) || "∅"}`).join(" / ") || "更新",
      comment: r.comment,
    })),
    ...p.audit.map((a) => ({ at: a.at, actor: a.actor, text: ACTION_LABELS[a.action] || a.action, comment: a.comment })),
  ].sort((a, b) => (a.at < b.at ? 1 : -1)).slice(0, 30);
  if (!items.length) return h("p", { class: "muted small" }, "履歴はありません");
  return h("ul", { class: "timeline" }, items.map((i) => h("li", {},
    h("div", { class: "when" }, fmtTime(i.at), " ", i.actor), h("div", {}, i.text), i.comment ? h("div", { class: "muted" }, "💬 ", i.comment) : null)));
}
