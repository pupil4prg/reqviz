// レイヤー設定（追加・名前変更・並べ替え・削除）

import { api } from "../api.js";
import { confirmDialog, h, modal, showError, toast, fill } from "../ui.js";

export async function render(root, { sid }) {
  const page = h("div", { class: "page" });
  root.append(page);

  async function load() {
    const [layers, params] = await Promise.all([api.get(`/api/systems/${sid}/layers`), api.get(`/api/systems/${sid}/params`)]);
    const count = (id) => params.filter((p) => p.layer_id === id).length;
    fill(page, 
      h("div", { class: "row" }, h("h1", {}, "レイヤー"), h("span", { class: "spacer" }),
        h("button", { class: "primary", onclick: () => edit(null) }, "＋ レイヤー")),
      h("p", { class: "hint" }, "レイヤーは関連図で左から右に並びます。上流（業務要件）を左に、下流（インフラ設計）を右に置いてください。キーはパラメータ参照（キー.パラメータキー）に使われ、変更するとルールの式も自動で書き換わります。"),
      h("table", { class: "tbl" },
        h("tr", {}, ["順序", "キー", "名前", "パラメータ数", ""].map((x) => h("th", {}, x))),
        layers.map((l, i) => h("tr", {},
          h("td", {}, h("div", { class: "row" },
            h("button", { class: "small", disabled: i === 0, title: "上へ", onclick: () => move(layers, i, -1) }, "↑"),
            h("button", { class: "small", disabled: i === layers.length - 1, title: "下へ", onclick: () => move(layers, i, 1) }, "↓"))),
          h("td", {}, h("code", {}, l.key)), h("td", {}, l.name), h("td", { class: "num" }, count(l.id)),
          h("td", {}, h("div", { class: "row" },
            h("button", { class: "small", onclick: () => edit(l) }, "編集"),
            h("button", { class: "small danger", disabled: count(l.id) > 0, title: count(l.id) ? "パラメータがあるため削除できません" : "", onclick: () => remove(l) }, "削除")))))),
    );
  }

  async function move(layers, i, d) {
    const ids = layers.map((l) => l.id);
    [ids[i], ids[i + d]] = [ids[i + d], ids[i]];
    try { await api.post(`/api/systems/${sid}/layers/reorder`, { layer_ids: ids }); load(); } catch (e) { showError(e); }
  }

  function edit(l) {
    const key = h("input", { value: l ? l.key : "", placeholder: "英数字（例: app）" });
    const name = h("input", { value: l ? l.name : "", placeholder: "例: アプリケーション設計" });
    modal({
      title: l ? "レイヤーを編集" : "レイヤーを追加",
      body: [h("label", { class: "field" }, "キー", key), h("label", { class: "field" }, "名前", name)],
      buttons: [{ label: "キャンセル" }, {
        label: "保存", primary: true,
        onClick: async () => {
          if (l) await api.put(`/api/layers/${l.id}`, { key: key.value.trim(), name: name.value, row_version: l.row_version });
          else await api.post(`/api/systems/${sid}/layers`, { key: key.value.trim(), name: name.value });
          toast("保存しました");
          load();
        },
      }],
    });
  }

  async function remove(l) {
    if (!(await confirmDialog("レイヤーを削除", `レイヤー「${l.name}」を削除します。`, "削除", true))) return;
    try { await api.del(`/api/layers/${l.id}`); load(); } catch (e) { showError(e); }
  }

  await load();
}
