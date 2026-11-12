// 構成項目（選択肢 = 列挙データ、不定形データ）の一覧・編集・承認・履歴

import { api } from "../api.js";
import { refreshNav } from "../main.js";
import { confirmDialog, fmtAny, fmtTime, h, modal, parseTags, promptComment, showError, tagChips, toast, fill } from "../ui.js";

const STATUS = { draft: "下書き", in_review: "レビュー中", approved: "承認済み" };
const CTYPE = { text: "テキスト", markdown: "Markdown", json: "JSON", yaml: "YAML" };
const OP = { create: "作成", update: "更新", delete: "削除", restore: "復元" };
const lines = (text) => text.split("\n").map((x) => x.trim()).filter(Boolean);

export async function render(root, { sid }) {
  const page = h("div", { class: "page" });
  root.append(page);
  let layers = [];

  async function load() {
    const [ls, items] = await Promise.all([api.get(`/api/systems/${sid}/layers`), api.get(`/api/systems/${sid}/items`)]);
    layers = ls;
    refreshNav(sid);
    const choices = items.filter((i) => i.kind === "choice");
    const docs = items.filter((i) => i.kind === "document");
    fill(page,
      h("div", { class: "row" }, h("h1", {}, "構成項目"), h("span", { class: "spacer" }),
        h("button", { class: "primary", disabled: !layers.length, onclick: () => edit(null, "choice") }, "＋ 選択肢"),
        h("button", { class: "primary", disabled: !layers.length, onclick: () => edit(null, "document") }, "＋ 不定形データ")),
      h("p", { class: "hint" }, "パラメータ（単一の値）で表せない構成情報を管理します。選択肢は実装方式などの候補と採用案で、採用案のキーはルールの式から",
        h("code", {}, " レイヤー.キー == 'redis' "), "のように参照できます。不定形データは、テキスト・Markdown・YAML・任意の JSON をそのまま保存します。",
        "どちらも変更履歴が残り、版（ベースライン）に含まれます。"),
      layers.length ? null : h("div", { class: "alert warn small" }, "先にレイヤーを作成してください。"),

      h("h2", {}, `選択肢（${choices.length}）`),
      choices.length ? choices.map(choiceCard) : h("p", { class: "muted" }, "選択肢はありません"),
      h("h2", {}, `不定形データ（${docs.length}）`),
      docs.length ? docs.map(docCard) : h("p", { class: "muted" }, "不定形データはありません"),
    );
  }

  function head(i) {
    return h("div", { class: "row" },
      h("h3", { style: { margin: 0 } }, i.name), h("code", {}, i.ref),
      h("span", { class: `badge ${i.status}` }, STATUS[i.status]),
      tagChips(i.tags),
      h("span", { class: "spacer" }),
      i.status === "draft" ? h("button", { class: "small", onclick: () => act(i, "request_review", "レビュー依頼") }, "レビュー依頼") : null,
      i.status === "in_review" ? [
        h("button", { class: "small primary", onclick: () => act(i, "approve", "承認") }, "承認"),
        h("button", { class: "small danger", onclick: () => act(i, "reject", "差し戻し", true) }, "差し戻し"),
        h("button", { class: "small", onclick: () => act(i, "withdraw", "取り下げ") }, "取り下げ")] : null,
      h("button", { class: "small", onclick: () => edit(i, i.kind) }, "編集"),
      h("button", { class: "small", onclick: () => showHistory(i) }, "履歴"),
      h("button", { class: "small danger", onclick: () => remove(i) }, "削除"));
  }

  function foot(i) {
    return h("div", { class: "small muted", style: { marginTop: "6px" } },
      i.rationale ? h("div", {}, "根拠: ", i.rationale) : null,
      [i.ref_doc ? `出典: ${i.ref_doc}` : "", i.owner ? `担当: ${i.owner}` : "", `更新: ${fmtTime(i.updated_at)}`].filter(Boolean).join(" ／ "));
  }

  function choiceCard(i) {
    return h("div", { class: "card" }, head(i),
      h("table", { class: "tbl", style: { marginTop: "8px" } },
        h("tr", {}, ["採用", "キー", "名前", "説明", "長所", "短所"].map((x) => h("th", {}, x))),
        i.options.map((o) => h("tr", { class: o.key === i.selected ? "selected-option" : "" },
          h("td", {}, o.key === i.selected
            ? h("span", { class: "badge approved" }, "採用")
            : h("button", { class: "small", onclick: () => select(i, o) }, "採用する")),
          h("td", {}, h("code", {}, o.key)), h("td", {}, o.label), h("td", { class: "small" }, o.description),
          h("td", { class: "small" }, o.pros.map((x) => h("div", {}, "＋ ", x))),
          h("td", { class: "small" }, o.cons.map((x) => h("div", {}, "－ ", x)))))),
      i.selected === null ? h("div", { class: "alert warn small" }, "採用する選択肢が未決定です。") : null,
      foot(i));
  }

  function docCard(i) {
    const body = typeof i.content === "string" ? i.content : JSON.stringify(i.content, null, 2);
    return h("div", { class: "card" }, head(i),
      h("div", { class: "small muted", style: { marginTop: "4px" } }, CTYPE[i.content_type]),
      h("pre", { class: "doc" }, body || "（空）"), foot(i));
  }

  async function act(i, action, label, needComment) {
    let comment = "";
    if (needComment) {
      comment = await promptComment({ title: `${label}: ${i.ref}`, required: true, okLabel: label, danger: true });
      if (comment === null) return;
    }
    try {
      await api.post(`/api/items/${i.id}/transition`, { action, comment, row_version: i.row_version });
      toast(`${label}しました`);
      load();
    } catch (e) { showError(e); }
  }

  async function select(i, o) {
    const comment = await promptComment({
      title: `「${o.label}」を採用`, placeholder: "選定理由（履歴に残ります）", okLabel: "採用する",
      message: i.status === "draft" ? "" : "内容を変更すると、この項目は下書きに戻ります。式で参照している下流のパラメータには要再計算が付きます。",
    });
    if (comment === null) return;
    try {
      await api.put(`/api/items/${i.id}`, { selected: o.key, comment, row_version: i.row_version });
      toast("採用案を変更しました");
      load();
    } catch (e) { showError(e); }
  }

  async function remove(i) {
    if (!(await confirmDialog("構成項目を削除", `「${i.name}」（${i.ref}）を削除します。履歴には残り、履歴から作り直せます。`, "削除", true))) return;
    try { await api.del(`/api/items/${i.id}`); load(); } catch (e) { showError(e); }
  }

  function optionRow(o, radioName, selected) {
    const row = h("tr", {},
      h("td", {}, h("input", { type: "radio", name: radioName, checked: o.key !== "" && o.key === selected, title: "採用する" })),
      h("td", {}, h("input", { value: o.key, placeholder: "redis", dataset: { f: "key" } })),
      h("td", {}, h("input", { value: o.label === o.key ? "" : o.label, placeholder: "表示名", dataset: { f: "label" } })),
      h("td", {}, h("textarea", { dataset: { f: "description" }, placeholder: "説明" }, o.description)),
      h("td", {}, h("textarea", { dataset: { f: "pros" }, placeholder: "1 行に 1 つ" }, o.pros.join("\n"))),
      h("td", {}, h("textarea", { dataset: { f: "cons" }, placeholder: "1 行に 1 つ" }, o.cons.join("\n"))),
      h("td", {}, h("button", { class: "small danger", title: "この選択肢を削除", onclick: () => row.remove() }, "×")));
    row.attributes_ = o.attributes || {};
    return row;
  }

  function edit(i, kind) {
    const layerSel = h("select", {}, layers.map((l) => h("option", { value: l.id, selected: i && i.layer_id === l.id }, `${l.name}（${l.key}）`)));
    const key = h("input", { value: i ? i.key : "", placeholder: kind === "choice" ? "英数字（例: session_store）" : "英数字（例: network_memo）" });
    const name = h("input", { value: i ? i.name : "", placeholder: kind === "choice" ? "例: セッション管理方式" : "例: ネットワーク構成メモ" });
    const tags = h("input", { value: i ? i.tags.join(", ") : "", placeholder: "カンマ区切り" });
    const rationale = h("textarea", { placeholder: kind === "choice" ? "選定理由" : "根拠・補足" }, i ? i.rationale : "");
    const refDoc = h("input", { value: i ? i.ref_doc : "" });
    const owner = h("input", { value: i ? i.owner : "" });
    const comment = h("input", { placeholder: "変更理由（履歴に残ります）" });

    const radioName = `sel-${Date.now()}`;
    const blank = { key: "", label: "", description: "", pros: [], cons: [], attributes: {} };
    const optBody = h("tbody", {}, (i ? i.options : [blank, { ...blank }]).map((o) => optionRow(o, radioName, i && i.selected)));
    const ctype = h("select", {}, Object.entries(CTYPE).map(([k, v]) => h("option", { value: k, selected: i && i.content_type === k }, v)));
    const content = h("textarea", { class: "mono", style: { minHeight: "260px" }, placeholder: "本文" },
      i ? (typeof i.content === "string" ? i.content : JSON.stringify(i.content, null, 2)) : "");

    const specific = kind === "choice"
      ? [h("div", { class: "table-wrap" }, h("table", { class: "tbl options-edit" },
          h("thead", {}, h("tr", {}, ["採用", "キー", "名前", "説明", "長所", "短所", ""].map((x) => h("th", {}, x)))), optBody)),
        h("div", { class: "row", style: { margin: "6px 0 12px" } },
          h("button", { class: "small", onclick: () => optBody.append(optionRow({ ...blank }, radioName, null)) }, "＋ 選択肢を追加"),
          h("span", { class: "hint" }, "採用が未決定なら、どれも選ばずに保存できます。"))]
      : [h("label", { class: "field" }, "形式", ctype), h("label", { class: "field" }, "本文", content)];

    modal({
      title: `${kind === "choice" ? "選択肢" : "不定形データ"}を${i ? "編集" : "追加"}`,
      wide: true,
      body: [
        h("div", { class: "grid2" }, h("label", { class: "field" }, "レイヤー", layerSel), h("label", { class: "field" }, "キー", key)),
        h("label", { class: "field" }, "名前", name),
        specific,
        h("label", { class: "field" }, kind === "choice" ? "選定理由" : "根拠", rationale),
        h("div", { class: "grid2" }, h("label", { class: "field" }, "出典", refDoc), h("label", { class: "field" }, "担当", owner)),
        h("label", { class: "field" }, "タグ", tags),
        i ? h("label", { class: "field" }, "変更理由", comment) : null,
        i && i.status !== "draft" ? h("div", { class: "alert warn small" }, "内容（選択肢・採用案・本文）を変更すると、下書きに戻ります。") : null,
      ],
      buttons: [{ label: "キャンセル" }, {
        label: "保存", primary: true,
        onClick: async () => {
          const data = {
            layer_id: Number(layerSel.value), key: key.value.trim(), name: name.value, kind,
            tags: parseTags(tags.value),
            rationale: rationale.value, ref_doc: refDoc.value, owner: owner.value, comment: comment.value,
          };
          if (kind === "choice") {
            data.selected = null;
            data.options = [...optBody.children].map((row) => {
              const f = (n) => row.querySelector(`[data-f="${n}"]`).value;
              const o = { key: f("key").trim(), label: f("label").trim(), description: f("description"), pros: lines(f("pros")), cons: lines(f("cons")), attributes: row.attributes_ };
              if (row.querySelector("input[type=radio]").checked) data.selected = o.key;
              return o;
            }).filter((o) => o.key);
          } else {
            data.content_type = ctype.value;
            data.content = content.value;
          }
          if (i) { delete data.kind; await api.put(`/api/items/${i.id}`, { ...data, row_version: i.row_version }); }
          else await api.post(`/api/systems/${sid}/items`, data);
          toast("保存しました");
          load();
        },
      }],
    });
  }

  async function showHistory(i) {
    let detail;
    try { detail = await api.get(`/api/items/${i.id}`); } catch (e) { return showError(e); }
    const latest = detail.revisions.length ? detail.revisions[0].id : null;
    let dlg = null;
    const restore = async (r) => {
      if (!(await confirmDialog("この版に戻す", `「${i.name}」を第 ${r.version} 版の内容に戻します。戻した操作も履歴に残ります。`, "戻す"))) return;
      try { await api.post(`/api/revisions/${r.id}/restore`, {}); toast("復元しました"); dlg.close(); load(); } catch (e) { showError(e); }
    };
    dlg = modal({
      title: `履歴: ${i.name}（${i.ref}）`, wide: true,
      body: h("table", { class: "tbl" },
        h("tr", {}, ["版", "日時", "操作者", "操作", "変更内容", "コメント", ""].map((x) => h("th", {}, x))),
        detail.revisions.map((r) => h("tr", {},
          h("td", { class: "num" }, r.version), h("td", { class: "small" }, fmtTime(r.at)), h("td", { class: "small" }, r.actor),
          h("td", {}, OP[r.op] || r.op),
          h("td", { class: "small" }, r.changes.length
            ? r.changes.map((c) => h("div", {}, h("b", {}, c.field), ": ", fmtAny(c.before) || "∅", " → ", fmtAny(c.after) || "∅"))
            : (r.op === "create" ? "（新規作成）" : "")),
          h("td", { class: "small" }, r.comment),
          h("td", {}, r.id === latest ? h("span", { class: "muted small" }, "現在") : h("button", { class: "small", onclick: () => restore(r) }, "この版に戻す"))))),
    });
  }

  await load();
}
