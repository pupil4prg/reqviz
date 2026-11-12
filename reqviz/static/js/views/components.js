// コンポーネント（構成要素）と、コンポーネント間の依存関係

import { api } from "../api.js";
import { confirmDialog, h, modal, s, showError, toast, fill } from "../ui.js";

const LINK_LABELS = { derives: "算出式", constrains: "制約", influences: "関連" };
const NODE_W = 180, NODE_H = 58, COL = 250, ROW = 84, PAD = 28;

export async function render(root, { sid }) {
  const page = h("div", { class: "page" });
  root.append(page);
  let data = null;

  async function load() {
    data = await api.get(`/api/systems/${sid}/components/analysis`);
    const { components: comps, deps, undeclared, unassigned, cycles } = data;
    const nameOf = Object.fromEntries(comps.map((c) => [c.key, c.name]));
    const unassignedCount = unassigned.params.length + unassigned.items.length;
    fill(page,
      h("div", { class: "row" }, h("h1", {}, "コンポーネント"), h("span", { class: "spacer" }),
        h("button", { class: "primary", onclick: () => editComponent(null) }, "＋ コンポーネント"),
        h("button", { class: "primary", disabled: comps.length < 2, onclick: () => editDep(null) }, "＋ 依存関係")),
      h("p", { class: "hint" }, "レイヤーが設計工程（業務要件 → … → インフラ設計）の軸なのに対して、コンポーネントは AP サーバー・ミドルウェア・OS のような構成要素の軸です。",
        "依存関係「A → B」は、A の設計が B に波及することを表します（例: ミドルウェア → OS）。定義した依存は、式・制約・関連による実際のつながりと突き合わせて確認できます。"),
      comps.length ? null : h("div", { class: "empty" }, "コンポーネントはまだありません。「＋ コンポーネント」で作成し、パラメータ一覧から割り当ててください。"),

      comps.length ? h("div", { class: "card comp-diagram" }, diagram(comps, deps, undeclared),
        h("div", { class: "legend-inline small muted" },
          h("span", {}, h("i", { class: "ln solid" }), "定義した依存"), h("span", {}, h("i", { class: "ln notify" }), "変更を知らせる依存"),
          h("span", {}, h("i", { class: "ln weak" }), "裏付けのない依存"), h("span", {}, h("i", { class: "ln undeclared" }), "未定義のつながり"))) : null,

      cycles.length ? h("div", { class: "alert warn small" }, "依存が循環しています: ", cycles[0].map((k) => nameOf[k] || k).join(" → "),
        "。互いに波及し合う設計なら問題ありませんが、意図していなければ向きを見直してください。") : null,
      undeclared.length ? h("div", { class: "alert warn" },
        h("b", {}, `定義されていないつながり（${undeclared.length}）`),
        h("div", { class: "small" }, "式・制約・関連ではつながっているのに、コンポーネント間の依存として定義されていません。"),
        h("table", { class: "tbl", style: { marginTop: "6px" } }, undeclared.map((u) => h("tr", {},
          h("td", {}, nameOf[u.from], u.undirected ? " ↔ " : " → ", nameOf[u.to]),
          h("td", { class: "small" }, linkList(u.links)),
          h("td", {}, h("div", { class: "row" },
            h("button", { class: "small", onclick: () => editDep(null, u.from, u.to) }, `${nameOf[u.from]} → ${nameOf[u.to]} として定義`),
            u.undirected ? h("button", { class: "small", onclick: () => editDep(null, u.to, u.from) }, "逆向きで定義") : null)))))) : null,
      comps.length && unassignedCount ? h("div", { class: "alert small" },
        `コンポーネントが未設定の項目が ${unassignedCount} 件あります（パラメータ ${unassigned.params.length} / 構成項目 ${unassigned.items.length}）。`,
        h("a", { href: `#/s/${sid}/params?component=-` }, " パラメータ一覧で割り当てる")) : null,

      comps.length ? [h("h2", {}, "コンポーネント"),
        h("table", { class: "tbl" },
          h("tr", {}, ["キー", "名前", "種別", "説明", "パラメータ", "構成項目", "波及元", "波及先", ""].map((x) => h("th", {}, x))),
          comps.map((c) => h("tr", {},
            h("td", {}, h("code", {}, c.key)), h("td", {}, h("a", { href: `#/s/${sid}/params?component=${encodeURIComponent(c.key)}` }, c.name)),
            h("td", { class: "small" }, c.kind), h("td", { class: "small" }, c.description),
            h("td", { class: "num" }, c.param_count), h("td", { class: "num" }, c.item_count),
            h("td", { class: "small" }, c.upstream.map((k) => nameOf[k]).join("、")),
            h("td", { class: "small" }, c.downstream.map((k) => nameOf[k]).join("、")),
            h("td", {}, h("div", { class: "row" },
              h("button", { class: "small", onclick: () => editComponent(c) }, "編集"),
              h("button", { class: "small danger", onclick: () => removeComponent(c) }, "削除"))))))] : null,

      deps.length ? [h("h2", {}, "依存関係"),
        h("table", { class: "tbl" },
          h("tr", {}, ["波及元", "", "波及先", "説明", "変更を知らせる", "裏付けとなるつながり", ""].map((x) => h("th", {}, x))),
          deps.map((d) => h("tr", {},
            h("td", {}, d.from_name), h("td", {}, "→"), h("td", {}, d.to_name), h("td", { class: "small" }, d.note),
            h("td", {}, h("label", { class: "row small", style: { flexWrap: "nowrap", whiteSpace: "nowrap" }, title: "波及元の値が変わったら、波及先の承認済みパラメータに「承認後に上流変更」を付けます" },
              h("input", { type: "checkbox", checked: d.notify, onchange: (e) => toggleNotify(d, e.target.checked) }), d.notify ? "する" : "しない")),
            h("td", { class: "small" }, d.supported ? linkList(d.links) : h("span", { class: "muted" }, "なし（式・制約・関連では確認できません）")),
            h("td", {}, h("div", { class: "row" },
              h("button", { class: "small", onclick: () => editDep(d) }, "編集"),
              h("button", { class: "small danger", onclick: () => removeDep(d) }, "削除"))))))] : null,
    );
  }

  function linkList(links) {
    const shown = links.slice(0, 4).map((l) => h("div", {},
      h("span", { class: "muted" }, `[${LINK_LABELS[l.kind]}${l.rule_key ? ` ${l.rule_key}` : ""}] `),
      h("code", {}, l.from_ref), l.kind === "constrains" ? " ↔ " : " → ", h("code", {}, l.to_ref)));
    return [shown, links.length > 4 ? h("div", { class: "muted" }, `ほか ${links.length - 4} 件`) : null];
  }

  function diagram(comps, deps, undeclared) {
    const cols = {};
    for (const c of comps) (cols[c.level] = cols[c.level] || []).push(c);
    const pos = {};
    const maxRows = Math.max(...Object.values(cols).map((x) => x.length));
    for (const [level, list] of Object.entries(cols)) {
      const offset = (maxRows - list.length) * ROW / 2;
      list.forEach((c, i) => { pos[c.key] = { x: PAD + level * COL, y: PAD + offset + i * ROW }; });
    }
    const width = PAD * 2 + (Math.max(...comps.map((c) => c.level)) + 1) * COL - (COL - NODE_W);
    const height = PAD * 2 + maxRows * ROW - (ROW - NODE_H) + 30;
    const path = (a, b, bend) => {
      const p = pos[a], q = pos[b];
      if (q.x > p.x) {
        const x1 = p.x + NODE_W, y1 = p.y + NODE_H / 2 + bend, x2 = q.x - 6, y2 = q.y + NODE_H / 2 + bend, mx = (x1 + x2) / 2;
        return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
      }
      if (q.x === p.x) {  // 同じ列は、右側にふくらませてつなぐ
        const x = p.x + NODE_W, y1 = p.y + NODE_H / 2, y2 = q.y + NODE_H / 2, out = 46 + Math.abs(bend) * 2;
        return `M${x},${y1} C${x + out},${y1} ${x + out},${y2} ${x + 6},${y2}`;
      }
      // 逆向き（循環）は、下側を回り込ませる
      const x1 = p.x + NODE_W / 2 + 20, y1 = p.y + NODE_H, x2 = q.x + NODE_W / 2 - 20, y2 = q.y + NODE_H + 6;
      const drop = Math.max(y1, y2) + 34 + Math.abs(bend) * 2;
      return `M${x1},${y1} C${x1},${drop} ${x2},${drop} ${x2},${y2}`;
    };
    const marker = (id, cls) => s("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
      s("path", { d: "M0,0 L10,5 L0,10 z", class: cls }));
    const edges = [
      ...deps.map((d) => {
        const cls = d.notify ? "notify" : d.supported ? "solid" : "weak";
        return s("path", { d: path(d.from_key, d.to_key, 0), class: `comp-edge ${cls}`, "marker-end": `url(#arr-${cls})` },
          s("title", {}, `${d.from_name} → ${d.to_name}${d.note ? `\n${d.note}` : ""}\n裏付け: ${d.links.length} 件`));
      }),
      ...undeclared.map((u) => s("path", {
        d: path(u.from, u.to, 9), class: "comp-edge undeclared", "marker-end": "url(#arr-undeclared)", "marker-start": u.undirected ? "url(#arr-undeclared)" : undefined,
      }, s("title", {}, `未定義のつながり（${u.links.length} 件）`))),
    ];
    return s("svg", { width, height, viewBox: `0 0 ${width} ${height}`, class: "comp-svg" },
      s("defs", {}, ["solid", "notify", "weak", "undeclared"].map((c) => marker(`arr-${c}`, `arrow ${c}`))),
      edges,
      comps.map((c) => s("g", { transform: `translate(${pos[c.key].x},${pos[c.key].y})`, class: "comp-node" },
        s("rect", { width: NODE_W, height: NODE_H, rx: 8 }),
        s("text", { x: 12, y: 23, class: "name" }, c.name.length > 13 ? c.name.slice(0, 12) + "…" : c.name),
        s("text", { x: 12, y: 43, class: "meta" }, `${c.kind || c.key} ・ ${c.param_count + c.item_count} 件`),
        s("title", {}, `${c.name}（${c.key}）\n${c.description || ""}`))));
  }

  function editComponent(c) {
    const key = h("input", { value: c ? c.key : "", placeholder: "英数字（例: postgres）" });
    const name = h("input", { value: c ? c.name : "", placeholder: "例: PostgreSQL" });
    const kind = h("input", { value: c ? c.kind : "", placeholder: "例: ミドルウェア", list: "comp-kinds" });
    const desc = h("textarea", {}, c ? c.description : "");
    modal({
      title: c ? "コンポーネントを編集" : "コンポーネントを追加",
      body: [
        h("datalist", { id: "comp-kinds" }, ["アプリケーション", "ミドルウェア", "OS", "ハードウェア", "ネットワーク", "クラウドサービス"].map((x) => h("option", { value: x }))),
        h("div", { class: "grid2" }, h("label", { class: "field" }, "キー", key), h("label", { class: "field" }, "種別", kind)),
        h("label", { class: "field" }, "名前", name), h("label", { class: "field" }, "説明", desc)],
      buttons: [{ label: "キャンセル" }, {
        label: "保存", primary: true,
        onClick: async () => {
          const body = { key: key.value.trim(), name: name.value, kind: kind.value, description: desc.value };
          if (c) await api.put(`/api/components/${c.id}`, { ...body, row_version: c.row_version });
          else await api.post(`/api/systems/${sid}/components`, body);
          toast("保存しました");
          load();
        },
      }],
    });
  }

  async function removeComponent(c) {
    const n = c.param_count + c.item_count;
    if (!(await confirmDialog("コンポーネントを削除", `「${c.name}」を削除します。${n ? `所属している ${n} 件の項目は未設定に戻り、` : ""}このコンポーネントに関する依存関係も削除されます。`, "削除", true))) return;
    try { await api.del(`/api/components/${c.id}`); load(); } catch (e) { showError(e); }
  }

  function editDep(d, from, to) {
    const opts = (sel) => data.components.map((c) => h("option", { value: c.key, selected: c.key === sel }, c.name));
    const fromSel = h("select", { disabled: !!d }, opts(d ? d.from_key : from));
    const toSel = h("select", { disabled: !!d }, opts(d ? d.to_key : to || (data.components[1] || {}).key));
    const note = h("textarea", { placeholder: "どう波及するか（例: 共有メモリとオープンファイル数の設定に影響する）" }, d ? d.note : "");
    const notify = h("input", { type: "checkbox", checked: d ? d.notify : false });
    modal({
      title: d ? "依存関係を編集" : "依存関係を定義",
      body: [
        h("div", { class: "grid2" }, h("label", { class: "field" }, "波及元", fromSel), h("label", { class: "field" }, "波及先", toSel)),
        h("label", { class: "field" }, "説明", note),
        h("label", { class: "row" }, notify, "波及元の値が変わったら、波及先に知らせる"),
        h("p", { class: "hint" }, "有効にすると、波及元のパラメータや採用案が変わったとき、波及先の承認済みパラメータに「承認後に上流変更」が付き、承認画面で確認を求めます。",
          "算出式でつながっているパラメータには、この設定に関係なく要再計算が付きます。")],
      buttons: [{ label: "キャンセル" }, {
        label: "保存", primary: true,
        onClick: async () => {
          if (d) await api.put(`/api/component-deps/${d.id}`, { note: note.value, notify: notify.checked });
          else await api.post(`/api/systems/${sid}/component-deps`, { from: fromSel.value, to: toSel.value, note: note.value, notify: notify.checked });
          toast("保存しました");
          load();
        },
      }],
    });
  }

  async function toggleNotify(d, on) {
    try { await api.put(`/api/component-deps/${d.id}`, { notify: on }); load(); } catch (e) { showError(e); load(); }
  }

  async function removeDep(d) {
    if (!(await confirmDialog("依存関係を削除", `${d.from_name} → ${d.to_name} の依存を削除します。`, "削除", true))) return;
    try { await api.del(`/api/component-deps/${d.id}`); load(); } catch (e) { showError(e); }
  }

  await load();
}
