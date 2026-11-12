// ログイン画面と、初期パスワードの変更

import { api } from "../api.js";
import { h, fill } from "../ui.js";

/** onDone: ログイン（とパスワード変更）が済んだときに呼ぶ */
export function render(root, { user, onDone }) {
  const box = h("div", { class: "login-box card" });
  root.append(h("div", { class: "login-page" }, box));
  if (user && user.must_change_password) changeForm(box, user, onDone);
  else loginForm(box, onDone);
}

function form(box, { title, lead, fields, submitLabel, onSubmit, footer }) {
  const err = h("div", { class: "alert error small", role: "alert", hidden: true });
  const btn = h("button", { class: "primary", type: "submit", style: { width: "100%", justifyContent: "center" } }, submitLabel);
  const el = h("form", { autocomplete: "on" }, h("h1", {}, title), lead ? h("p", { class: "small muted" }, lead) : null, err, fields, btn, footer || null);
  el.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    err.hidden = true;
    btn.disabled = true;
    try { await onSubmit(); }
    catch (e) { err.textContent = e.message || String(e); err.hidden = false; }
    finally { btn.disabled = false; }
  });
  fill(box, el);
  const first = el.querySelector("input");
  if (first) first.focus();
}

function loginForm(box, onDone) {
  const id = h("input", { name: "username", autocomplete: "username", required: true, autocapitalize: "none", spellcheck: "false" });
  const pw = h("input", { name: "password", type: "password", autocomplete: "current-password", required: true });
  form(box, {
    title: "ログイン",
    fields: [h("label", { class: "field" }, "ID", id), h("label", { class: "field" }, "パスワード", pw)],
    submitLabel: "ログイン",
    onSubmit: async () => {
      try { await api.post("/api/auth/login", { login: id.value.trim(), password: pw.value }); }
      catch (e) { pw.value = ""; pw.focus(); throw e; }
      await onDone();
    },
    footer: h("p", { class: "hint", style: { marginTop: "14px" } }, "アカウントは管理者が作成します。ID やパスワードが分からない場合は、管理者に問い合わせてください。"),
  });
}

function changeForm(box, user, onDone) {
  const cur = h("input", { type: "password", autocomplete: "current-password", required: true });
  const next = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8 });
  const again = h("input", { type: "password", autocomplete: "new-password", required: true, minlength: 8 });
  form(box, {
    title: "パスワードの変更",
    lead: `${user.display_name}（${user.login}）さん、管理者が設定した初期パスワードを、自分だけが知るパスワードに変更してください。`,
    fields: [
      h("label", { class: "field" }, "現在のパスワード（初期パスワード）", cur),
      h("label", { class: "field" }, "新しいパスワード（8 文字以上）", next),
      h("label", { class: "field" }, "新しいパスワード（確認）", again),
    ],
    submitLabel: "変更して続ける",
    onSubmit: async () => {
      if (next.value !== again.value) throw new Error("新しいパスワードが確認用と一致しません");
      await api.post("/api/auth/password", { current_password: cur.value, new_password: next.value });
      await onDone();
    },
    footer: h("p", { style: { marginTop: "14px", textAlign: "center" } },
      h("a", { href: "#", class: "link-muted small", onclick: async (ev) => { ev.preventDefault(); try { await api.post("/api/auth/logout"); } catch { /* 済んでいる */ } await onDone(); } }, "別のアカウントでログインする")),
  });
}
