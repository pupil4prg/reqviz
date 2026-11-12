// ユーザー管理（管理者のみ）: アカウントの作成・ロール変更・パスワード再設定・無効化・削除

import { api } from "../api.js";
import { app } from "../main.js";
import { confirmDialog, fmtTime, h, modal, showError, toast, fill } from "../ui.js";

export async function render(root) {
  const page = h("div", { class: "page" });
  root.append(page);
  const roles = app.auth.roles;
  const me = app.auth.user;

  const roleSelect = (value) => h("select", {}, Object.entries(roles).map(([k, v]) => h("option", { value: k, selected: k === value }, v)));
  const pwInput = () => h("input", { type: "password", autocomplete: "new-password", placeholder: "8 文字以上" });

  async function load() {
    const users = await api.get("/api/users");
    fill(page,
      h("div", { class: "row" }, h("h1", {}, "ユーザー管理"), h("span", { class: "spacer" }),
        h("button", { class: "primary", onclick: create }, "＋ アカウントを作成")),
      h("p", { class: "hint" }, "アカウントを作成できるのは管理者だけです。作成時に決めた初期パスワードを本人に伝えてください。本人は最初のログイン時に、必ず自分のパスワードに変更します。",
        "承認・差し戻しは承認者と管理者、アカウント管理と LLM の接続設定は管理者だけが行えます。"),
      h("table", { class: "tbl" },
        h("tr", {}, ["ID", "表示名", "ロール", "状態", "作成日時", ""].map((x) => h("th", {}, x))),
        users.map((u) => h("tr", {},
          h("td", {}, h("code", {}, u.login), u.id === me.id ? h("span", { class: "badge" }, "自分") : null),
          h("td", {}, u.display_name),
          h("td", {}, roles[u.role] || u.role),
          h("td", {}, u.disabled ? h("span", { class: "badge error" }, "無効")
            : u.must_change_password ? h("span", { class: "badge in_review", title: "まだ初期パスワードのままです" }, "初期パスワード")
              : h("span", { class: "badge approved" }, "有効")),
          h("td", { class: "small" }, fmtTime(u.created_at)),
          h("td", {}, h("div", { class: "row" },
            h("button", { class: "small", onclick: () => edit(u) }, "編集"),
            h("button", { class: "small", onclick: () => resetPassword(u) }, "パスワード再設定"),
            h("button", { class: "small", disabled: u.id === me.id, onclick: () => toggle(u) }, u.disabled ? "有効にする" : "無効にする"),
            h("button", { class: "small danger", disabled: u.id === me.id, onclick: () => remove(u) }, "削除")))))),
    );
  }

  function create() {
    const login = h("input", { placeholder: "英数字 . _ -（例: yamada）", autocomplete: "off", autocapitalize: "none" });
    const name = h("input", { placeholder: "例: 山田 太郎" });
    const role = roleSelect("designer");
    const pw = pwInput();
    modal({
      title: "アカウントを作成",
      body: [
        h("label", { class: "field" }, "ID（ログインに使う。履歴の操作者名にもなる）", login),
        h("label", { class: "field" }, "表示名", name),
        h("label", { class: "field" }, "ロール", role),
        h("label", { class: "field" }, "初期パスワード", pw),
        h("p", { class: "hint" }, "初期パスワードは保存後に表示できません。本人に安全な方法で伝えてください。"),
      ],
      buttons: [{ label: "キャンセル" }, {
        label: "作成", primary: true,
        onClick: async () => {
          await api.post("/api/users", { login: login.value.trim(), display_name: name.value, role: role.value, password: pw.value });
          toast("アカウントを作成しました");
          load();
        },
      }],
    });
  }

  function edit(u) {
    const name = h("input", { value: u.display_name });
    const role = roleSelect(u.role);
    role.disabled = u.id === me.id;
    modal({
      title: `アカウントを編集: ${u.login}`,
      body: [h("label", { class: "field" }, "表示名", name), h("label", { class: "field" }, "ロール", role),
        u.id === me.id ? h("p", { class: "hint" }, "自分自身のロールは変更できません。") : null],
      buttons: [{ label: "キャンセル" }, {
        label: "保存", primary: true,
        onClick: async () => {
          const data = { display_name: name.value };
          if (u.id !== me.id) data.role = role.value;
          await api.put(`/api/users/${u.id}`, data);
          toast("保存しました");
          load();
        },
      }],
    });
  }

  function resetPassword(u) {
    const pw = pwInput();
    modal({
      title: `パスワードを再設定: ${u.login}`,
      body: [h("label", { class: "field" }, "新しい初期パスワード", pw),
        h("p", { class: "hint" }, "ログイン中の端末はログアウトされ、本人は次のログイン時にパスワードを変更します。")],
      buttons: [{ label: "キャンセル" }, {
        label: "再設定", primary: true,
        onClick: async () => { await api.put(`/api/users/${u.id}`, { password: pw.value }); toast("パスワードを再設定しました"); load(); },
      }],
    });
  }

  async function toggle(u) {
    if (!u.disabled && !(await confirmDialog("アカウントを無効にする", `${u.login} はログインできなくなり、発行済みの API トークンも使えなくなります。履歴は残ります。`, "無効にする", true))) return;
    try { await api.put(`/api/users/${u.id}`, { disabled: !u.disabled }); load(); } catch (e) { showError(e); }
  }

  async function remove(u) {
    if (!(await confirmDialog("アカウントを削除", `${u.login} を削除します。過去の履歴・監査ログの操作者名は残ります。一時的に止めるだけなら「無効にする」を使ってください。`, "削除", true))) return;
    try { await api.del(`/api/users/${u.id}`); toast("削除しました"); load(); } catch (e) { showError(e); }
  }

  await load();
}
