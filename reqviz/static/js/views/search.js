// 検索: パラメータと構成項目を、語とタグで横断して探す。タグの管理（名前の変更）もここで行う

import { api } from "../api.js";
import { aliasList, h, modal, showError, STATUS_LABELS, tagChips, toast, fill } from "../ui.js";

const TYPE_LABELS = { param: "パラメータ", choice: "選択肢", document: "不定形データ" };
const ITEM_STATUS = { draft: "下書き", in_review: "レビュー中", approved: "承認済み" };
const MATCH_LABELS = {
  ref: "参照名", name: "名前", alias: "別名", value: "値", unit: "単位", rationale: "根拠", ref_doc: "出典", owner: "担当",
  tag: "タグ", selected: "採用案", option: "選択肢", content: "本文",
};

export async function render(root, { sid, query }) {
  const page = h("div", { class: "page" });
  root.append(page);
  const state = {
    q: query.q || "", tags: new Set((query.tags || "").split(",").filter(Boolean)), mode: query.tag_mode || "all",
    kind: query.kind || "", status: query.status || "",
  };
  const resultsBox = h("div");
  const tagsBox = h("div", { class: "row", style: { margin: "8px 0" } });
  let timer = null;

  function queryString() {
    const p = new URLSearchParams();
    if (state.q.trim()) p.set("q", state.q.trim());
    if (state.tags.size) { p.set("tags", [...state.tags].join(",")); if (state.mode === "any") p.set("tag_mode", "any"); }
    if (state.kind) p.set("kind", state.kind);
    if (state.status) p.set("status", state.status);
    return p.toString();
  }

  async function run() {
    const qs = queryString();
    history.replaceState(null, "", `#/s/${sid}/search${qs ? `?${qs}` : ""}`);  // 検索条件を URL に残す（共有・再読み込み用）
    try {
      const r = await api.get(`/api/systems/${sid}/search?${qs}`);
      fill(resultsBox,
        h("p", { class: "small muted" }, `${r.total} 件${r.total > r.results.length ? `（先頭 ${r.results.length} 件を表示）` : ""}`),
        r.results.length ? h("table", { class: "tbl" },
          h("tr", {}, ["種類", "名前", "値・採用案", "タグ", "状態", "一致した項目"].map((x) => h("th", {}, x))),
          r.results.map((x) => h("tr", {},
            h("td", { class: "small" }, TYPE_LABELS[x.type]),
            h("td", {}, h("a", { href: x.type === "param" ? `#/s/${sid}/graph?ref=${encodeURIComponent(x.ref)}` : `#/s/${sid}/items` }, x.name),
              h("div", { class: "mono small muted" }, x.ref), aliasList(x.aliases)),
            h("td", { class: "small" }, x.summary),
            h("td", {}, tagChips(x.tags, toggleTag, state.tags)),
            h("td", {}, h("span", { class: `badge ${x.status}` }, (x.type === "param" ? STATUS_LABELS : ITEM_STATUS)[x.status]),
              x.flag_stale ? h("span", { class: "badge stale" }, "要再計算") : null),
            h("td", { class: "small muted" }, x.matched.map((m) => MATCH_LABELS[m] || m).join("、")))))
          : h("div", { class: "empty" }, "一致するものはありません"));
    } catch (e) { fill(resultsBox, h("div", { class: "alert error" }, e.message)); }
  }

  function toggleTag(t) {
    if (state.tags.has(t)) state.tags.delete(t); else state.tags.add(t);
    renderTags();
    run();
  }

  let known = [];
  function renderTags() {
    fill(tagsBox,
      h("span", { class: "small muted" }, "タグ:"),
      known.length
        ? known.map((c) => h("span", { class: `tag clickable ${state.tags.has(c.tag) ? "active" : ""}`, onclick: () => toggleTag(c.tag) },
          c.tag, h("span", { class: "count" }, c.params + c.items)))
        : h("span", { class: "small muted" }, "まだありません。パラメータや構成項目の編集画面で付けられます。"),
      state.tags.size > 1 ? h("label", { class: "row small" },
        h("input", { type: "checkbox", checked: state.mode === "any", onchange: (e) => { state.mode = e.target.checked ? "any" : "all"; run(); } }),
        "いずれかが付いているもの") : null,
      known.length ? h("button", { class: "small link", onclick: renameDialog }, "タグ名を変更…") : null);
  }

  async function loadTags() {
    known = await api.get(`/api/systems/${sid}/tags`);
    for (const t of [...state.tags]) if (!known.some((c) => c.tag === t)) state.tags.delete(t);
    renderTags();
  }

  function renameDialog() {
    const from = h("select", {}, known.map((c) => h("option", { value: c.tag }, `${c.tag}（${c.params + c.items}）`)));
    const to = h("input", { placeholder: "新しいタグ名" });
    modal({
      title: "タグ名を変更",
      body: [h("label", { class: "field" }, "変更するタグ", from), h("label", { class: "field" }, "新しい名前", to),
        h("p", { class: "hint" }, "このシステムのすべてのパラメータ・構成項目で置き換えます。既にあるタグ名を指定すると、そのタグに統合されます。")],
      buttons: [{ label: "キャンセル" }, {
        label: "変更", primary: true,
        onClick: async () => {
          const res = await api.post(`/api/systems/${sid}/tags/rename`, { from: from.value, to: to.value });
          toast(`${res.updated.length} 件のタグを変更しました`);
          state.tags.delete(from.value);
          await loadTags();
          run();
        },
      }],
    });
  }

  const input = h("input", { type: "search", value: state.q, placeholder: "検索語（空白区切りで、すべてを含むもの）", style: { flex: "1", minWidth: "260px" } });
  input.addEventListener("input", () => { state.q = input.value; clearTimeout(timer); timer = setTimeout(run, 200); });
  const kindSel = h("select", {}, [["", "すべての種類"], ["param", "パラメータ"], ["choice", "選択肢"], ["document", "不定形データ"]]
    .map(([v, l]) => h("option", { value: v, selected: state.kind === v }, l)));
  kindSel.addEventListener("change", () => { state.kind = kindSel.value; run(); });
  const statusSel = h("select", {}, [["", "すべての状態"], ["draft", "下書き"], ["ai_evaluated", "AI評価済み"], ["in_review", "レビュー中"], ["approved", "承認済み"], ["stale", "要再計算"]]
    .map(([v, l]) => h("option", { value: v, selected: state.status === v }, l)));
  statusSel.addEventListener("change", () => { state.status = statusSel.value; run(); });

  fill(page,
    h("h1", {}, "検索"),
    h("p", { class: "hint" }, "パラメータと構成項目を横断して探します。参照名・名前・別名・値・根拠・出典・担当・タグ・選択肢・本文が対象です。",
      "タグは「開発中」「要確認」のような作業状況の管理に使えます（承認の状態とは独立です）。"),
    h("div", { class: "row" }, input, kindSel, statusSel),
    tagsBox, resultsBox);
  try { await loadTags(); } catch (e) { showError(e); }
  await run();
  input.focus();
  return () => clearTimeout(timer);
}
