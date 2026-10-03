// システム一覧・テンプレート管理

import { api } from "../api.js";
import { navigate } from "../main.js";
import { confirmDialog, download, fmtTime, h, modal, readJsonFile, showError, toast, fill } from "../ui.js";

export async function render(root) {
  const page = h("div", { class: "page" });
  root.append(page);

  async function load() {
    const [systems, templates] = await Promise.all([api.get("/api/systems"), api.get("/api/templates")]);
    fill(page, 
      h("div", { class: "row" }, h("h1", {}, "システム"), h("span", { class: "spacer" }),
        h("button", { class: "primary", onclick: () => createDialog(templates) }, "＋ 新規システム")),
      systems.length
        ? h("div", { class: "cards" }, systems.map(systemCard))
        : h("div", { class: "empty card" }, "システムがありません。テンプレートから作成してみてください。"),
      h("div", { class: "row" }, h("h2", {}, "テンプレート"), h("span", { class: "spacer" }),
        h("button", { onclick: importTemplate }, "JSON から取り込み")),
      h("p", { class: "hint" }, "テンプレートはシステム作成時にコピーされます。テンプレートを更新しても、作成済みのシステムには反映されません。"),
      templates.length ? templateTable(templates) : h("div", { class: "empty card" }, "テンプレートはありません"),
    );
  }

  function systemCard(s) {
    const pct = s.param_count ? Math.round((s.approved_count / s.param_count) * 100) : 0;
    return h("div", { class: "card" },
      h("h3", {}, h("a", { href: `#/s/${s.id}/graph` }, s.name)),
      s.description ? h("p", { class: "muted small" }, s.description) : null,
      h("div", { class: "small" }, `パラメータ ${s.param_count} 件（承認済み ${s.approved_count} 件・${pct}%）`),
      h("div", { class: "small muted" }, s.current_baseline_version ? `現行ベースライン v${s.current_baseline_version}` : "承認済みの版なし",
        " ／ 更新 ", fmtTime(s.updated_at)),
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("button", { class: "small", onclick: () => navigate(`#/s/${s.id}/graph`) }, "開く"),
        h("button", { class: "small", onclick: () => cloneDialog(s) }, "複製"),
        h("span", { class: "spacer" }),
        h("button", { class: "small danger", onclick: () => removeSystem(s) }, "削除")),
    );
  }

  function templateTable(templates) {
    return h("table", { class: "tbl" },
      h("tr", {}, ["名前", "版", "内容", "説明", "登録日時", ""].map((x) => h("th", {}, x))),
      templates.map((t) => h("tr", {},
        h("td", {}, t.name), h("td", {}, `v${t.version}`),
        h("td", { class: "small" }, `レイヤー ${t.summary.layers} / パラメータ ${t.summary.params} / ルール ${t.summary.rules}`),
        h("td", { class: "small muted" }, t.description), h("td", { class: "small" }, fmtTime(t.created_at)),
        h("td", {}, h("div", { class: "row" },
          h("button", { class: "small", onclick: () => download(`/api/templates/${t.id}/export`) }, "書き出し"),
          h("button", { class: "small danger", onclick: () => removeTemplate(t) }, "削除"))),
      )));
  }

  function createDialog(templates) {
    const name = h("input", { placeholder: "例: 販売管理システム" });
    const desc = h("textarea", { placeholder: "説明（任意）" });
    const tpl = h("select", {}, h("option", { value: "" }, "空のシステム"),
      templates.map((t) => h("option", { value: t.id }, `${t.name} v${t.version}`)));
    if (templates.length) tpl.value = templates[0].id;
    modal({
      title: "新規システム",
      body: [h("label", { class: "field" }, "システム名", name), h("label", { class: "field" }, "テンプレート", tpl),
        h("label", { class: "field" }, "説明", desc)],
      buttons: [{ label: "キャンセル" }, {
        label: "作成", primary: true,
        onClick: async () => {
          const s = await api.post("/api/systems", { name: name.value, description: desc.value, template_id: tpl.value ? Number(tpl.value) : null });
          toast("システムを作成しました");
          navigate(`#/s/${s.id}/graph`);
        },
      }],
    });
  }

  function cloneDialog(s) {
    const name = h("input", { value: `${s.name} のコピー` });
    modal({
      title: "システムを複製",
      body: [h("p", { class: "small muted" }, "パラメータ・ルール・状態をそのまま複製します。版と履歴は複製しません。"),
        h("label", { class: "field" }, "新しいシステム名", name)],
      buttons: [{ label: "キャンセル" }, {
        label: "複製", primary: true,
        onClick: async () => { await api.post(`/api/systems/${s.id}/clone`, { name: name.value }); toast("複製しました"); load(); },
      }],
    });
  }

  async function removeSystem(s) {
    if (!(await confirmDialog("システムを削除", `「${s.name}」と、そのパラメータ・ルール・版・履歴をすべて削除します。元に戻せません。`, "削除", true))) return;
    try { await api.del(`/api/systems/${s.id}`); toast("削除しました"); load(); } catch (e) { showError(e); }
  }

  async function removeTemplate(t) {
    if (!(await confirmDialog("テンプレートを削除", `「${t.name} v${t.version}」を削除します。作成済みのシステムには影響しません。`, "削除", true))) return;
    try { await api.del(`/api/templates/${t.id}`); load(); } catch (e) { showError(e); }
  }

  async function importTemplate() {
    try {
      const payload = await readJsonFile();
      if (!payload) return;
      const t = await api.post("/api/templates", { payload });
      toast(`テンプレート「${t.name} v${t.version}」を取り込みました`);
      load();
    } catch (e) { showError(e); }
  }

  await load();
}
