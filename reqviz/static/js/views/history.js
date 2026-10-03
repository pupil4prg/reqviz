// 変更履歴と監査ログ

import { api } from "../api.js";
import { fmtTime, fmtValue, h, fill } from "../ui.js";

const ENTITY_LABELS = { param: "パラメータ", rule: "ルール", layer: "レイヤー", relation: "関連", system: "システム全体" };
const OP_LABELS = { create: "作成", update: "更新", delete: "削除", restore: "復元" };

export async function render(root, { sid, query }) {
  const page = h("div", { class: "page" });
  root.append(page);
  let tab = query.tab || "revisions";
  let entity = "";

  async function load() {
    const tabs = h("div", { class: "tabs" },
      h("button", { class: tab === "revisions" ? "active" : "", onclick: () => { tab = "revisions"; load(); } }, "変更履歴"),
      h("button", { class: tab === "audit" ? "active" : "", onclick: () => { tab = "audit"; load(); } }, "監査ログ"));
    let content;
    if (tab === "revisions") {
      const sel = h("select", {}, h("option", { value: "" }, "すべて"), Object.entries(ENTITY_LABELS).map(([k, v]) => h("option", { value: k, selected: entity === k }, v)));
      sel.addEventListener("change", () => { entity = sel.value; load(); });
      const revs = await api.get(`/api/systems/${sid}/revisions?limit=300${entity ? `&entity=${entity}` : ""}`);
      content = [h("div", { class: "row", style: { marginBottom: "8px" } }, sel),
        h("table", { class: "tbl" },
          h("tr", {}, ["日時", "操作者", "対象", "操作", "変更内容", "コメント"].map((x) => h("th", {}, x))),
          revs.map((r) => h("tr", {},
            h("td", { class: "small" }, fmtTime(r.at)), h("td", { class: "small" }, r.actor),
            h("td", {}, h("span", { class: "muted small" }, ENTITY_LABELS[r.entity] || r.entity), " ", h("code", {}, r.entity_key)),
            h("td", {}, OP_LABELS[r.op] || r.op),
            h("td", { class: "small" }, r.changes.length
              ? r.changes.map((c) => h("div", {}, h("b", {}, c.field), ": ", fmtValue(c.before) || "∅", " → ", fmtValue(c.after) || "∅"))
              : summarize(r)),
            h("td", { class: "small" }, r.comment))))];
    } else {
      const logs = await api.get(`/api/systems/${sid}/audit?limit=300`);
      content = h("table", { class: "tbl" },
        h("tr", {}, ["日時", "操作者", "操作", "対象", "詳細", "コメント"].map((x) => h("th", {}, x))),
        logs.map((a) => h("tr", {},
          h("td", { class: "small" }, fmtTime(a.at)), h("td", { class: "small" }, a.actor),
          h("td", {}, h("code", {}, a.action)), h("td", {}, a.target_key ? h("code", {}, a.target_key) : ""),
          h("td", { class: "small mono" }, a.detail ? JSON.stringify(a.detail) : ""), h("td", { class: "small" }, a.comment))));
    }
    fill(page, h("h1", {}, "履歴"), tabs, content);
  }

  await load();
}

function summarize(r) {
  const obj = r.after || r.before;
  if (!obj || typeof obj !== "object") return "";
  return Object.entries(obj).slice(0, 4).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : fmtValue(v)}`).join(" / ");
}
