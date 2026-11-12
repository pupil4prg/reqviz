// ルーター（ハッシュ方式）と画面の枠

import { api, currentUser, setCsrfToken } from "./api.js";
import { clear, h, showError, fill } from "./ui.js";
import * as systemsView from "./views/systems.js";
import * as graphView from "./views/graph-view.js";
import * as paramsView from "./views/params.js";
import * as itemsView from "./views/items.js";
import * as rulesView from "./views/rules.js";
import * as layersView from "./views/layers.js";
import * as approvalsView from "./views/approvals.js";
import * as baselinesView from "./views/baselines.js";
import * as historyView from "./views/history.js";
import * as aiView from "./views/ai.js";
import * as settingsView from "./views/settings.js";
import * as searchView from "./views/search.js";
import * as componentsView from "./views/components.js";
import * as loginView from "./views/login.js";
import * as usersView from "./views/users.js";

const SYSTEM_TABS = [
  ["graph", "関連図", graphView],
  ["params", "パラメータ", paramsView],
  ["items", "構成項目", itemsView],
  ["search", "検索", searchView],
  ["rules", "ルール", rulesView],
  ["layers", "レイヤー", layersView],
  ["components", "コンポーネント", componentsView],
  ["approvals", "承認", approvalsView],
  ["baselines", "版管理", baselinesView],
  ["ai", "AI", aiView],
  ["history", "履歴", historyView],
];

let cleanup = null;
export const app = { meta: null, system: null, auth: { auth_enabled: false, user: null, roles: {} } };

/** ログインが必要な状態か（未ログイン、または初期パスワードの変更待ち） */
function needsLogin() {
  const a = app.auth;
  return a.auth_enabled && (!a.user || a.user.must_change_password);
}

/** サーバーのログイン状態を取り直し、上部バーの表示を合わせる */
export async function refreshAuth() {
  app.auth = await api.get("/api/auth/me");
  setCsrfToken(app.auth.csrf_token);
  const { auth_enabled: enabled, user } = app.auth;
  const ready = !needsLogin();
  app.meta = ready ? await api.get("/api/meta") : null;
  const badge = document.getElementById("user-badge");
  badge.textContent = enabled ? (user && ready ? user.display_name : "") : (currentUser() || (app.meta && app.meta.user) || "");
  badge.title = enabled && user ? `${user.login}（${app.auth.roles[user.role] || user.role}）` : "操作者（設定で変更）";
  badge.hidden = !badge.textContent;
  document.getElementById("logout-btn").hidden = !(enabled && user);
  document.getElementById("settings-link").hidden = !ready;
  document.getElementById("users-link").hidden = !(ready && enabled && user && user.role === "admin");
}

async function logout() {
  try { await api.post("/api/auth/logout"); } catch { /* セッションが既に切れている */ }
  app.meta = null;
  await refreshAuth();
  route();
}

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
    const pending = queue.params.length + queue.items.length + queue.baselines.length + queue.upstream_changed.length;
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
  try {
    if (needsLogin()) {
      refreshNav(null);
      document.title = "ログイン - 要件パラメータ可視化";
      loginView.render(root, { user: app.auth.user, onDone: async () => { await refreshAuth(); route(); } });
    } else if (parts[0] === "users" && app.auth.auth_enabled && app.auth.user.role === "admin") {
      refreshNav(null);
      cleanup = (await usersView.render(root, { query })) || null;
      document.title = "ユーザー管理 - 要件パラメータ可視化";
    } else if (parts[0] === "s" && parts[1]) {
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
  try { await refreshAuth(); } catch (e) { showError(e); }
  window.addEventListener("hashchange", route);
  // セッション切れなどで API が 401 を返したら、ログイン画面に戻す
  let handling = false;
  window.addEventListener("reqviz:auth-required", async () => {
    if (handling) return;
    handling = true;
    try { await refreshAuth(); route(); } catch (e) { showError(e); } finally { handling = false; }
  });
  document.getElementById("logout-btn").addEventListener("click", logout);
  route();
}

init();
