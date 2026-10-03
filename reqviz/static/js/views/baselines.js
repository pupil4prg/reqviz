// 版管理（作成・承認・差し戻し・差分・復元）

import { api } from "../api.js";
import { refreshNav } from "../main.js";
import { BASELINE_LABELS, confirmDialog, fmtTime, fmtValue, h, modal, promptComment, showError, toast, fill } from "../ui.js";

const SECTION_LABELS = { params: "パラメータ", rules: "ルール", layers: "レイヤー", relations: "関連" };
const CHANGE_LABELS = { added: "追加", removed: "削除", modified: "変更" };

export async function render(root, { sid }) {
  const page = h("div", { class: "page" });
  root.append(page);

  async function load() {
    const { baselines, readiness } = await api.get(`/api/systems/${sid}/baselines`);
    const label = h("input", { placeholder: "版の名前（例: 基本設計 FIX）", style: { width: "280px" } });
    fill(page, 
      h("h1", {}, "版管理"),
      h("div", { class: "card" },
        h("h3", {}, "新しい版を作成してレビューに出す"),
        h("p", { class: "small muted" }, "作成時点のパラメータ・ルール・関連・レイヤーをすべて凍結して保存します。承認済みの版は変更できません。"),
        readiness.ready
          ? h("div", { class: "alert ok small" }, "版を作成できます。",
            readiness.warnings.length ? h("div", {}, `warning の制約違反が ${readiness.warnings.length} 件あります（承認時にコメントが必要です）。`) : null)
          : h("div", { class: "alert warn small" }, "まだ版を作成できません:", h("ul", {}, readiness.blockers.map((b) => h("li", {}, b)))),
        h("div", { class: "row" }, label,
          h("button", { class: "primary", disabled: !readiness.ready, onclick: () => create(label.value) }, "版を作成"),
          h("button", { onclick: () => showDiff("current", "working", "現行ベースライン → 作業中") }, "現行ベースラインとの差分"))),
      h("h2", {}, "版の一覧"),
      baselines.length
        ? h("table", { class: "tbl" },
          h("tr", {}, ["版", "状態", "作成", "判断", "warning", ""].map((x) => h("th", {}, x))),
          baselines.map((b) => h("tr", {},
            h("td", {}, h("b", {}, `v${b.version_no}`), " ", b.label),
            h("td", {}, h("span", { class: `badge ${b.status}` }, BASELINE_LABELS[b.status])),
            h("td", { class: "small" }, b.created_by, h("div", { class: "muted" }, fmtTime(b.created_at))),
            h("td", { class: "small" }, b.decided_by || "", h("div", { class: "muted" }, fmtTime(b.decided_at)),
              b.decision_comment ? h("div", {}, "💬 ", b.decision_comment) : null),
            h("td", { class: "num" }, b.warnings.length || ""),
            h("td", {}, h("div", { class: "row" },
              b.status === "in_review" ? h("button", { class: "small primary", onclick: () => decide(b, true) }, "承認") : null,
              b.status === "in_review" ? h("button", { class: "small danger", onclick: () => decide(b, false) }, "差し戻し") : null,
              h("button", { class: "small", onclick: () => showDiff(String(b.id), "working", `v${b.version_no} → 作業中`) }, "作業中との差分"),
              h("button", { class: "small", onclick: () => restore(b) }, "この版に戻す"))))))
        : h("p", { class: "muted" }, "版はまだありません"),
    );
  }

  async function create(label) {
    try {
      const b = await api.post(`/api/systems/${sid}/baselines`, { label });
      toast(`版 v${b.version_no} を作成し、レビューに出しました`);
      refreshNav(sid, "baselines");
      load();
    } catch (e) { showError(e); }
  }

  async function decide(b, approve) {
    const needComment = !approve || b.warnings.length > 0;
    let comment = "";
    if (needComment || approve) {
      comment = await promptComment({
        title: approve ? `版 v${b.version_no} を承認` : `版 v${b.version_no} を差し戻し`,
        message: approve && b.warnings.length ? `warning の制約違反が ${b.warnings.length} 件あります: ${b.warnings.map((w) => w.rule_key).join(", ")}。承認理由を入力してください。` : "",
        required: needComment, okLabel: approve ? "承認" : "差し戻し", danger: !approve,
      });
      if (comment === null) return;
    }
    try {
      await api.post(`/api/baselines/${b.id}/${approve ? "approve" : "reject"}`, { comment });
      toast(approve ? "承認しました" : "差し戻しました");
      refreshNav(sid, "baselines");
      load();
    } catch (e) { showError(e); }
  }

  async function restore(b) {
    if (!(await confirmDialog("版に戻す", `作業中の内容を v${b.version_no} の内容で置き換えます。現在の作業内容は履歴に残りますが、元に戻すには別の版から復元する必要があります。`, "戻す", true))) return;
    try {
      await api.post(`/api/baselines/${b.id}/restore`, {});
      toast(`v${b.version_no} の内容に戻しました`);
      load();
    } catch (e) { showError(e); }
  }

  async function showDiff(from, to, title) {
    try {
      const d = await api.get(`/api/systems/${sid}/diff?from=${from}&to=${to}`);
      modal({ title: `差分: ${title}`, wide: true, body: diffBody(d) });
    } catch (e) { showError(e); }
  }

  await load();
}

export function diffBody(d) {
  const sections = Object.entries(d).filter(([, items]) => items.length);
  if (!sections.length) return h("p", {}, "差分はありません。");
  return sections.map(([sec, items]) => [
    h("h3", {}, `${SECTION_LABELS[sec] || sec}（${items.length}）`),
    h("table", { class: "tbl" }, items.map((x) => h("tr", {},
      h("td", { style: { width: "220px" } }, h("code", {}, x.key)),
      h("td", { class: `diff-${x.change}`, style: { width: "60px" } }, CHANGE_LABELS[x.change]),
      h("td", { class: "small" }, x.change === "modified"
        ? x.fields.map((f) => h("div", {}, h("b", {}, f.field), ": ", fmtValue(f.before) || "∅", " → ", fmtValue(f.after) || "∅"))
        : "")))),
  ]);
}
