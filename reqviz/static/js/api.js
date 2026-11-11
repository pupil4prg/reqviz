// API クライアント

export class ApiError extends Error {
  constructor(status, body) {
    const err = (body && body.error) || {};
    super(err.message || `HTTP ${status}`);
    this.status = status;
    this.code = err.code;
    this.details = err.details;
  }
}

/** ログイン認証が無効なときだけ使う、自己申告の操作者名 */
export function currentUser() {
  try { return localStorage.getItem("reqviz.user") || ""; } catch { return ""; }
}

export function setCurrentUser(name) {
  try { localStorage.setItem("reqviz.user", name); } catch { /* 保存できなくても動作は継続 */ }
}

let csrfToken = null;
/** ログイン後にサーバーから受け取った CSRF トークン（メモリ上にだけ持つ） */
export function setCsrfToken(token) { csrfToken = token || null; }

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const user = currentUser();
  if (user) headers["X-Reqviz-User"] = encodeURIComponent(user);
  if (csrfToken && method !== "GET") headers["X-CSRF-Token"] = csrfToken;
  const res = await fetch(path, { method, headers, credentials: "same-origin", body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) {
    const err = new ApiError(res.status, data);
    // セッション切れ・初期パスワードの変更待ちは、ログイン画面に戻す
    const authLost = (res.status === 401 && path !== "/api/auth/login") || err.code === "password_change_required";
    if (authLost) window.dispatchEvent(new Event("reqviz:auth-required"));
    throw err;
  }
  return data;
}

export const api = {
  get: (p) => request("GET", p),
  post: (p, b = {}) => request("POST", p, b),
  put: (p, b = {}) => request("PUT", p, b),
  del: (p) => request("DELETE", p),
};
