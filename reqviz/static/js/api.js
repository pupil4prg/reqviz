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

export function currentUser() {
  try { return localStorage.getItem("reqviz.user") || ""; } catch { return ""; }
}

export function setCurrentUser(name) {
  try { localStorage.setItem("reqviz.user", name); } catch { /* 保存できなくても動作は継続 */ }
}

async function request(method, path, body) {
  const headers = { "Content-Type": "application/json" };
  const user = currentUser();
  if (user) headers["X-Reqviz-User"] = encodeURIComponent(user);
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const text = await res.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = null; }
  if (!res.ok) throw new ApiError(res.status, data);
  return data;
}

export const api = {
  get: (p) => request("GET", p),
  post: (p, b = {}) => request("POST", p, b),
  put: (p, b = {}) => request("PUT", p, b),
  del: (p) => request("DELETE", p),
};
