// 承認キュー（レビュー中のパラメータ・承認後に上流変更があったもの・レビュー中の版）

import { api } from "../api.js";
import { refreshNav } from "../main.js";
import { fmtTime, fmtValue, h, promptComment, showError, toast, fill } from "../ui.js";

export async function render(root, { sid }) {
  const page = h("div", { class: "page" });
  root.append(page);

  async function act(p, action, label, needComment) {
    let comment = "";
    if (needComment) {
      comment = await promptComment({ title: `${label}: ${p.ref}`, required: true, okLabel: label, danger: action === "reject" });
      if (comment === null) return;
    }
    try {
      await api.post(`/api/params/${p.id}/transition`, { action, comment, row_version: p.row_version });
      toast(`${label}しました`);
      load();
    } catch (e) { showError(e); }
  }

  async function approveAll(params) {
    try {
      const res = await api.post(`/api/systems/${sid}/transition`, { param_ids: params.map((p) => p.id), action: "approve" });
      toast(`${res.ok.length} 件を承認しました`);
      load();
    } catch (e) { showError(e); }
  }

  async function load() {
    const q = await api.get(`/api/systems/${sid}/approvals`);
    refreshNav(sid, "approvals");
    fill(page, 
      h("h1", {}, "承認"),
      h("p", { class: "hint" }, "MVP では承認者は 1 名で、自己承認も可能です。承認・差し戻しはすべて監査ログに記録されます。"),

      h("div", { class: "row" }, h("h2", {}, `レビュー中のパラメータ（${q.params.length}）`), h("span", { class: "spacer" }),
        q.params.length ? h("button", { class: "small", onclick: () => approveAll(q.params) }, "すべて承認") : null),
      q.params.length
        ? h("table", { class: "tbl" },
          h("tr", {}, ["パラメータ", "値", "根拠", ""].map((x) => h("th", {}, x))),
          q.params.map((p) => h("tr", {},
            h("td", {}, h("a", { href: `#/s/${sid}/graph?ref=${encodeURIComponent(p.ref)}` }, p.name), h("div", { class: "mono small muted" }, p.ref),
              p.ai_skipped ? h("span", { class: "badge" }, "AIスキップ") : null),
            h("td", {}, fmtValue(p.value), " ", p.unit),
            h("td", { class: "small" }, p.rationale),
            h("td", {}, h("div", { class: "row" },
              h("button", { class: "small primary", onclick: () => act(p, "approve", "承認", false) }, "承認"),
              h("button", { class: "small danger", onclick: () => act(p, "reject", "差し戻し", true) }, "差し戻し"))))))
        : h("p", { class: "muted" }, "レビュー中のパラメータはありません"),

      h("h2", {}, `承認後に上流が変更されたもの（${q.upstream_changed.length}）`),
      q.upstream_changed.length
        ? h("table", { class: "tbl" },
          h("tr", {}, ["パラメータ", "値", "フラグ", ""].map((x) => h("th", {}, x))),
          q.upstream_changed.map((p) => h("tr", {},
            h("td", {}, h("a", { href: `#/s/${sid}/graph?ref=${encodeURIComponent(p.ref)}` }, p.name), h("div", { class: "mono small muted" }, p.ref)),
            h("td", {}, fmtValue(p.value), " ", p.unit, p.has_proposal ? h("div", { class: "small", style: { color: "var(--stale)" } }, `再計算案: ${fmtValue(p.proposed_value)}`) : null),
            h("td", {}, p.flag_stale ? h("span", { class: "badge stale" }, "要再計算") : null),
            h("td", {}, p.status === "approved"
              ? h("button", { class: "small", onclick: () => act(p, "ack_upstream", "確認済みに", true) }, "影響なしとして確認済みにする")
              : h("span", { class: "muted small" }, "関連図で再計算・再承認してください")))))
        : h("p", { class: "muted" }, "ありません"),

      h("h2", {}, `レビュー中の版（${q.baselines.length}）`),
      q.baselines.length
        ? h("ul", {}, q.baselines.map((b) => h("li", {}, h("a", { href: `#/s/${sid}/baselines` }, `v${b.version_no} ${b.label || ""}`),
          h("span", { class: "muted small" }, ` ${b.created_by} / ${fmtTime(b.created_at)}`))))
        : h("p", { class: "muted" }, "ありません"),
    );
  }

  await load();
}
