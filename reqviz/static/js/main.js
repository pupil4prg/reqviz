// ルーター（ハッシュ方式）と画面の枠

import { api, currentUser } from "./api.js";
import { clear, h, showError, fill } from "./ui.js";
import * as systemsView from "./views/systems.js";
import * as graphView from "./views/graph-view.js";
import * as paramsView from "./views/params.js";
import * as rulesView from "./views/rules.js";
import * as layersView from "./views/layers.js";
import * as approvalsView from "./views/approvals.js";
import * as baselinesView from "./views/baselines.js";
import * as historyView from "./views/history.js";
import * as aiView from "./views/ai.js";
import * as settingsView from "./views/settings.js";

const SYSTEM_TABS = [
  ["graph", "関連図", graphView],
  ["params", "パラメータ", paramsView],
  ["rules", "ルール", rulesView],
  ["layers", "レイヤー", layersView],
  ["approvals", "承認", approvalsView],
  ["baselines", "版管理", baselinesView],
  ["ai", "AI", aiView],
  ["history", "履歴", historyView],
];

let cleanup = null;
export const app = { meta: null, system: null };

function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, qs] = raw.split("?");
  return { parts: path.split("/").filter(Boolean), query: Object.fromEntries(new URLSearchParams(qs || "")) };
}

export function navigate(hash) { location.hash = hash; }

/** システムのナビゲーション（タブ）を再描画する。承認待ち件数も更新。 */
export async function refreshNav(sid) {
  const nav = document.getElementById("system-nav");
  if (!sid) { clear(nav); app.system = null; return; }
  try {
    const [system, queue] = await Promise.all([api.get(`/api/systems/${sid}`), api.get(`/api/systems/${sid}/approvals`)]);
    // 応答待ちの間に画面が切り替わっていても、現在の URL に合わせて描画する
    const { parts } = parseHash();
    if (parts[0] !== "s" || Number(parts[1]) !== sid) return;
    const active = parts[2] || "graph";
    app.system = system;
    const pending = queue.params.length + queue.baselines.length + queue.upstream_changed.length;
    fill(nav, 
      h("span", { class: "sys-name", title: system.name }, system.name),
      SYSTEM_TABS.map(([key, label]) => h("a", { href: `#/s/${sid}/${key}`, class: key === active ? "active" : "" },
        label, key === "approvals" && pending ? h("span", { class: "badge in_review" }, pending) : null)),
    );
  } catch (e) { showError(e); }
}

async function route() {
  if (cleanup) { try { cleanup(); } catch { /* noop */ } cleanup = null; }
  const root = clear(document.getElementById("app"));
  const { parts, query } = parseHash();
  document.getElementById("user-badge").textContent = currentUser() || (app.meta && app.meta.user) || "";
  try {
    if (parts[0] === "s" && parts[1]) {
      const sid = Number(parts[1]);
      const tab = parts[2] || "graph";
      const entry = SYSTEM_TABS.find(([k]) => k === tab) || SYSTEM_TABS[0];
      refreshNav(sid, entry[0]);
      cleanup = (await entry[2].render(root, { sid, query })) || null;
      const name = app.system ? app.system.name + " - " : "";
      document.title = `${entry[1]} - ${name}要件パラメータ可視化`;
    } else if (parts[0] === "settings") {
      refreshNav(null);
      cleanup = (await settingsView.render(root, { query })) || null;
      document.title = "設定 - 要件パラメータ可視化";
    } else {
      refreshNav(null);
      cleanup = (await systemsView.render(root, { query })) || null;
      document.title = "要件パラメータ可視化";
    }
  } catch (e) {
    showError(e);
    root.append(h("div", { class: "page" }, h("div", { class: "alert error" }, e.message || String(e))));
  }
}

async function init() {
  try { app.meta = await api.get("/api/meta"); } catch (e) { showError(e); }
  window.addEventListener("hashchange", route);
  route();
}

init();
