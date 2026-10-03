// パラメータ一覧（表形式での一括編集）と、パラメータ作成ダイアログ

import { api } from "../api.js";
import { refreshNav } from "../main.js";
import {
  flagBadges, fmtValue, h, modal, readValue, showError, SOURCE_LABELS, statusBadge, toast, TYPE_LABELS, valueInput,
  fill,
} from "../ui.js";

const BULK_ACTIONS = [
  ["skip_ai", "AI評価をスキップ"],
  ["request_review", "レビュー依頼"],
  ["approve", "承認"],
  ["withdraw", "下書きに戻す"],
];

export async function render(root, { sid }) {
  const page = h("div", { class: "page" });
  root.append(page);
  let filterText = "";
  let filterStatus = "";

  async function load() {
    const graph = await api.get(`/api/systems/${sid}/graph`);
    const nodes = graph.nodes;
    const selected = new Set();
    const dirty = new Map(); // id -> {el, node}

    const search = h("input", { type: "search", placeholder: "絞り込み", value: filterText });
    const statusSel = h("select", {}, h("option", { value: "" }, "すべての状態"),
      [["draft", "下書き"], ["ai_evaluated", "AI評価済み"], ["in_review", "レビュー中"], ["approved", "承認済み"], ["stale", "要再計算"]]
        .map(([v, l]) => h("option", { value: v, selected: filterStatus === v }, l)));
    const tbody = h("tbody");
    const bulkSel = h("select", {}, h("option", { value: "" }, "選択したものを…"), BULK_ACTIONS.map(([v, l]) => h("option", { value: v }, l)));
    const saveBtn = h("button", { class: "primary", disabled: true, onclick: saveAll }, "変更を保存");

    function visible(n) {
      if (filterStatus === "stale" ? !n.flag_stale : filterStatus && n.status !== filterStatus) return false;
      const t = filterText.toLowerCase();
      return !t || [n.ref, n.name, n.rationale].some((v) => v && v.toLowerCase().includes(t));
    }

    function renderRows() {
      fill(tbody);
      for (const layer of graph.layers) {
        const rows = nodes.filter((n) => n.layer_key === layer.key && visible(n));
        if (!rows.length) continue;
        tbody.append(h("tr", { class: "group" }, h("td", { colspan: 9 }, layer.name)));
        for (const n of rows) tbody.append(row(n));
      }
    }

    function markDirty(n, field, el) {
      const d = dirty.get(n.id) || { node: n, fields: {} };
      d.fields[field] = el;
      dirty.set(n.id, d);
      el.style.background = "var(--warn-bg)";
      saveBtn.disabled = false;
      saveBtn.textContent = `変更を保存（${dirty.size}）`;
    }

    function row(n) {
      const cb = h("input", { type: "checkbox", checked: selected.has(n.id), onchange: (e) => (e.target.checked ? selected.add(n.id) : selected.delete(n.id)) });
      const val = valueInput(n, n.value);
      val.addEventListener("input", () => markDirty(n, "value", val));
      val.addEventListener("change", () => markDirty(n, "value", val));
      const unit = h("input", { value: n.unit, style: { width: "70px" } });
      unit.addEventListener("input", () => markDirty(n, "unit", unit));
      const rat = h("input", { value: n.rationale });
      rat.addEventListener("input", () => markDirty(n, "rationale", rat));
      return h("tr", {},
        h("td", {}, cb),
        h("td", {}, h("a", { href: `#/s/${sid}/graph?ref=${encodeURIComponent(n.ref)}` }, n.name), h("div", { class: "mono small muted" }, n.ref)),
        h("td", { class: "small" }, TYPE_LABELS[n.type]),
        h("td", { style: { minWidth: "130px" } }, n.formula ? h("div", { title: n.formula }, h("span", { class: "muted small" }, "ƒ "), fmtValue(n.value) || "∅",
          n.has_proposal ? h("div", { class: "small", style: { color: "var(--stale)" } }, `案: ${fmtValue(n.proposed_value)}`) : null) : val),
        h("td", {}, unit),
        h("td", { style: { minWidth: "200px" } }, rat),
        h("td", { class: "small" }, SOURCE_LABELS[n.source]),
        h("td", {}, statusBadge(n.status)),
        h("td", {}, h("div", { class: "row" }, flagBadges(n))));
    }

    async function saveAll() {
      saveBtn.disabled = true;
      let ok = 0;
      for (const [id, d] of dirty) {
        const body = { row_version: d.node.row_version };
        for (const [field, el] of Object.entries(d.fields)) body[field] = field === "value" ? readValue(d.node, el) : el.value;
        try { await api.put(`/api/params/${id}`, body); ok += 1; } catch (e) { showError(Object.assign(e, { message: `${d.node.ref}: ${e.message}` })); }
      }
      toast(`${ok} 件保存しました`);
      await load();
      refreshNav(sid, "params");
    }

    async function bulk() {
      const action = bulkSel.value;
      bulkSel.value = "";
      if (!action || !selected.size) return;
      try {
        const res = await api.post(`/api/systems/${sid}/transition`, { param_ids: [...selected], action });
        toast(`${res.ok.length} 件を処理しました${res.failed.length ? `（${res.failed.length} 件は対象外）` : ""}`, res.failed.length ? "error" : "info");
        if (res.failed.length) {
          const byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
          modal({
            title: "処理できなかったパラメータ",
            body: h("ul", {}, res.failed.map((f) => h("li", {}, h("code", {}, byId[f.id].ref), " ", f.message,
              f.details && f.details.reasons ? `（${f.details.reasons.join("、")}）` : ""))),
          });
        }
        await load();
        refreshNav(sid, "params");
      } catch (e) { showError(e); }
    }
    bulkSel.addEventListener("change", bulk);

    search.addEventListener("input", () => { filterText = search.value; renderRows(); });
    statusSel.addEventListener("change", () => { filterStatus = statusSel.value; renderRows(); });
    const allCb = h("input", {
      type: "checkbox", title: "表示中をすべて選択",
      onchange: (e) => { nodes.filter(visible).forEach((n) => (e.target.checked ? selected.add(n.id) : selected.delete(n.id))); renderRows(); },
    });

    renderRows();
    fill(page, 
      h("div", { class: "row" }, h("h1", {}, "パラメータ"), h("span", { class: "spacer" }),
        h("button", { onclick: () => paramDialog(sid, graph.layers, null, load) }, "＋ パラメータ")),
      h("div", { class: "row", style: { marginBottom: "10px" } }, search, statusSel, h("span", { class: "spacer" }), bulkSel, saveBtn),
      h("div", { class: "table-wrap" }, h("table", { class: "tbl" },
        h("thead", {}, h("tr", {}, h("th", {}, allCb), ["名前", "型", "値", "単位", "根拠", "出所", "状態", "フラグ"].map((x) => h("th", {}, x)))),
        tbody)),
      h("p", { class: "hint" }, "算出式で決まる値（ƒ）は、関連図の「再計算」から更新してください。値を変更すると、承認済みなどの状態は下書きに戻ります。"),
    );
  }

  await load();
}

/** パラメータ作成・定義編集ダイアログ（p が null なら新規） */
export function paramDialog(sid, layers, p, onDone) {
  const layer = h("select", {}, layers.map((l) => h("option", { value: l.id, selected: p && p.layer_id === l.id }, l.name)));
  const key = h("input", { value: p ? p.key : "", placeholder: "英数字（例: max_threads）" });
  const name = h("input", { value: p ? p.name : "", placeholder: "例: 最大スレッド数" });
  const type = h("select", {}, Object.entries(TYPE_LABELS).map(([v, l]) => h("option", { value: v, selected: p && p.type === v }, l)));
  const opts = h("input", { value: p ? p.enum_options.join(", ") : "", placeholder: "カンマ区切り（例: online, batch）" });
  const optsField = h("label", { class: "field" }, "選択肢", opts);
  const value = h("input", { value: p ? fmtValue(p.value) : "" });
  const unit = h("input", { value: p ? p.unit : "" });
  const rationale = h("textarea", {}, p ? p.rationale : "");
  const syncType = () => { optsField.style.display = type.value === "enum" ? "" : "none"; };
  type.addEventListener("change", syncType);
  syncType();
  if (!layers.length) { toast("先にレイヤーを作成してください", "error"); return; }
  modal({
    title: p ? "パラメータの定義を編集" : "パラメータを追加",
    body: [
      h("div", { class: "grid2" }, h("label", { class: "field" }, "レイヤー", layer), h("label", { class: "field" }, "キー", key)),
      h("label", { class: "field" }, "名前", name),
      h("div", { class: "grid2" }, h("label", { class: "field" }, "型", type), h("label", { class: "field" }, "単位", unit)),
      optsField,
      h("label", { class: "field" }, "値", value),
      h("label", { class: "field" }, "根拠", rationale),
      h("p", { class: "hint" }, "式からは「レイヤーキー.キー」で参照します。キーを変更すると、ルールの式も自動で書き換わります。"),
    ],
    buttons: [{ label: "キャンセル" }, {
      label: p ? "保存" : "追加", primary: true,
      onClick: async () => {
        const t = type.value;
        let v = value.value.trim();
        if (v === "") v = null;
        else if (t === "number") v = Number(v.replace(/,/g, "")) || (v === "0" ? 0 : v);
        else if (t === "bool") v = v === "true";
        const body = {
          layer_id: Number(layer.value), key: key.value.trim(), name: name.value, type: t,
          enum_options: t === "enum" ? opts.value.split(",").map((x) => x.trim()).filter(Boolean) : [],
          value: v, unit: unit.value, rationale: rationale.value,
        };
        if (p) await api.put(`/api/params/${p.id}`, { ...body, row_version: p.row_version });
        else await api.post(`/api/systems/${sid}/params`, body);
        toast(p ? "保存しました" : "追加しました");
        onDone && onDone();
      },
    }],
  });
}
