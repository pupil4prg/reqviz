// 変更履歴（復元つき）・時点間の差分・監査ログ

import { api } from "../api.js";
import { confirmDialog, fmtAny, fmtTime, h, showError, toast, fill } from "../ui.js";

const ENTITY_LABELS = { param: "パラメータ", item: "構成項目", rule: "ルール", layer: "レイヤー", relation: "関連", system: "システム全体" };
const OP_LABELS = { create: "作成", update: "更新", delete: "削除", restore: "復元" };
const SECTION_LABELS = { params: "パラメータ", items: "構成項目", rules: "ルール" };
const CHANGE_LABELS = { added: "追加", removed: "削除", modified: "変更" };
const RESTORABLE = new Set(["param", "item", "rule"]);

/** <input type="datetime-local"> 用の値（ローカル時刻） */
function localInput(d) {
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

export async function render(root, { sid, query }) {
  const page = h("div", { class: "page" });
  root.append(page);
  let tab = query.tab || "revisions";
  let entity = "";
  let from = localInput(new Date(Date.now() - 7 * 86400000));
  let to = "";

  async function restore(r) {
    const what = r.op === "delete" ? "削除前の内容で作り直します" : "この変更の直後の内容に戻します";
    if (!(await confirmDialog("内容を復元", `${ENTITY_LABELS[r.entity]} ${r.entity_key} を、${what}。復元も履歴に残り、値が変わると下書きに戻ります。`, "復元"))) return;
    try { await api.post(`/api/revisions/${r.id}/restore`, {}); toast("復元しました"); load(); } catch (e) { showError(e); }
  }

  async function revisionsTab() {
    const sel = h("select", {}, h("option", { value: "" }, "すべて"), Object.entries(ENTITY_LABELS).map(([k, v]) => h("option", { value: k, selected: entity === k }, v)));
    sel.addEventListener("change", () => { entity = sel.value; load(); });
    const revs = await api.get(`/api/systems/${sid}/revisions?limit=300${entity ? `&entity=${entity}` : ""}`);
    return [h("div", { class: "row", style: { marginBottom: "8px" } }, sel,
        h("span", { class: "hint" }, "パラメータ・構成項目・ルールは、過去の内容に 1 件ずつ戻せます。全体を戻す場合は版管理の復元を使います。")),
      h("table", { class: "tbl" },
        h("tr", {}, ["日時", "操作者", "対象", "操作", "変更内容", "コメント", ""].map((x) => h("th", {}, x))),
        revs.map((r) => h("tr", {},
          h("td", { class: "small" }, fmtTime(r.at)), h("td", { class: "small" }, r.actor),
          h("td", {}, h("span", { class: "muted small" }, ENTITY_LABELS[r.entity] || r.entity), " ", h("code", {}, r.entity_key)),
          h("td", {}, OP_LABELS[r.op] || r.op),
          h("td", { class: "small" }, r.changes.length
            ? r.changes.map((c) => h("div", {}, h("b", {}, c.field), ": ", fmtAny(c.before) || "∅", " → ", fmtAny(c.after) || "∅"))
            : summarize(r)),
          h("td", { class: "small" }, r.comment),
          h("td", {}, RESTORABLE.has(r.entity) ? h("button", { class: "small", onclick: () => restore(r) }, "復元") : null))))];
  }

  async function diffTab() {
    const fromEl = h("input", { type: "datetime-local", value: from });
    const toEl = h("input", { type: "datetime-local", value: to });
    const iso = (v) => new Date(v).toISOString();
    let result;
    try {
      const d = await api.get(`/api/systems/${sid}/history/diff?from=${encodeURIComponent(iso(from))}${to ? `&to=${encodeURIComponent(iso(to))}` : ""}`);
      const total = Object.keys(SECTION_LABELS).reduce((n, k) => n + d[k].length, 0);
      result = total === 0 ? h("p", { class: "muted" }, "この期間に内容の変更はありません。")
        : Object.entries(SECTION_LABELS).filter(([k]) => d[k].length).map(([k, label]) => [
          h("h3", {}, `${label}（${d[k].length}）`),
          h("table", { class: "tbl" }, d[k].map((x) => h("tr", {},
            h("td", { style: { width: "60px" } }, h("span", { class: `diff-${x.change}` }, CHANGE_LABELS[x.change])),
            h("td", { style: { width: "260px" } }, h("code", {}, x.key)),
            h("td", { class: "small" }, x.change === "modified"
              ? x.fields.map((f) => h("div", {}, h("b", {}, f.field), ": ", fmtAny(f.before) || "∅", " → ", fmtAny(f.after) || "∅"))
              : summarizeObj(x.after || x.before)))))]);
    } catch (e) { result = h("div", { class: "alert error" }, e.message); }
    return [
      h("div", { class: "row", style: { marginBottom: "8px" } },
        h("label", {}, "いつから ", fromEl), h("label", {}, "いつまで ", toEl),
        h("button", { class: "primary", onclick: () => { if (!fromEl.value) return toast("開始日時を指定してください", "error"); from = fromEl.value; to = toEl.value; load(); } }, "比較"),
        h("span", { class: "hint" }, "「いつまで」が空なら現在まで。承認などの状態の変化は監査ログを参照してください。")),
      result];
  }

  async function auditTab() {
    const logs = await api.get(`/api/systems/${sid}/audit?limit=300`);
    return h("table", { class: "tbl" },
      h("tr", {}, ["日時", "操作者", "操作", "対象", "詳細", "コメント"].map((x) => h("th", {}, x))),
      logs.map((a) => h("tr", {},
        h("td", { class: "small" }, fmtTime(a.at)), h("td", { class: "small" }, a.actor),
        h("td", {}, h("code", {}, a.action)), h("td", {}, a.target_key ? h("code", {}, a.target_key) : ""),
        h("td", { class: "small mono" }, a.detail ? JSON.stringify(a.detail) : ""), h("td", { class: "small" }, a.comment))));
  }

  async function load() {
    const btn = (key, label) => h("button", { class: tab === key ? "active" : "", onclick: () => { tab = key; load(); } }, label);
    const tabs = h("div", { class: "tabs" }, btn("revisions", "変更履歴"), btn("diff", "時点間の差分"), btn("audit", "監査ログ"));
    const content = tab === "diff" ? await diffTab() : tab === "audit" ? await auditTab() : await revisionsTab();
    fill(page, h("h1", {}, "履歴"), tabs, content);
  }

  await load();
}

function summarizeObj(obj) {
  if (!obj || typeof obj !== "object") return "";
  return Object.entries(obj).filter(([, v]) => v !== null && v !== "" && !(Array.isArray(v) && !v.length))
    .slice(0, 5).map(([k, v]) => `${k}: ${fmtAny(v, 60)}`).join(" / ");
}

function summarize(r) { return summarizeObj(r.after || r.before); }
