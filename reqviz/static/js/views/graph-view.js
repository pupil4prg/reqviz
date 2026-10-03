// 関連図（メイン画面）

import { api } from "../api.js";
import { PURPOSE_LABELS, startAi } from "../ai-run.js";
import { GraphCanvas } from "../graph/canvas.js";
import { refreshNav } from "../main.js";
import { renderParamPanel } from "../param-panel.js";
import { paramDialog } from "./params.js";
import { download, fmtValue, h, modal, showError, toast, fill } from "../ui.js";

export async function render(root, { sid, query }) {
  const page = h("div", { class: "graph-page" });
  const main = h("div", { class: "graph-main" });
  const toolbar = h("div", { class: "toolbar" });
  const canvasBox = h("div", { class: "graph-canvas" });
  const violationsBox = h("div", { class: "violations-panel" });
  const side = h("aside", { class: "side-panel" });
  main.append(toolbar, canvasBox, violationsBox);
  page.append(main, side);
  root.append(page);

  let graph = null;
  let selected = query.ref || null;
  let selectedKind = selected ? "param" : null;

  const canvas = new GraphCanvas(canvasBox, {
    storageKey: `reqviz.layout.${sid}`,
    onSelect: (id, kind, ev) => {
      if (ev && ev.shiftKey && kind === "param") { trace(id, "both"); return; }
      select(id, kind);
    },
  });
  canvasBox.append(legend(), h("div", { class: "zoom-ctl" },
    h("button", { title: "拡大", onclick: () => canvas.zoom(1.25) }, "＋"),
    h("button", { title: "縮小", onclick: () => canvas.zoom(0.8) }, "－"),
    h("button", { title: "全体を表示", onclick: () => canvas.fit() }, "⤢")));

  function trace(ref, dir) {
    canvas.highlight = ref ? canvas.trace(ref, dir) : null;
    canvas.draw();
  }

  async function reload() {
    graph = await api.get(`/api/systems/${sid}/graph`);
    canvas.selected = selectedKind === "param" ? selected : null;
    canvas.setData(graph);
    renderToolbar();
    renderViolations();
    await renderSide();
  }

  async function onChange(arg) {
    if (arg === null) { selected = null; selectedKind = null; }
    try { await reload(); refreshNav(sid, "graph"); } catch (e) { showError(e); }
  }

  async function select(id, kind) {
    selected = id;
    selectedKind = kind;
    canvas.selected = kind === "param" ? id : null;
    canvas.draw();
    await renderSide();
  }

  async function renderSide() {
    try {
      if (selectedKind === "param") {
        const n = graph.nodes.find((x) => x.ref === selected);
        if (!n) { selected = null; selectedKind = null; return renderSide(); }
        await renderParamPanel(side, n.id, { sid, graph, onChange, onTrace: trace });
      } else if (selectedKind === "rule") {
        renderRulePanel(graph.rule_nodes.find((r) => r.id === selected));
      } else {
        renderSummary();
      }
    } catch (e) { showError(e); }
  }

  function renderRulePanel(r) {
    if (!r) return renderSummary();
    fill(side, 
      h("h2", {}, `制約 ${r.key}`),
      h("div", { class: "row" }, h("span", { class: `badge ${r.severity}` }, r.severity), r.violation ? h("span", { class: "badge error" }, "違反") : h("span", { class: "badge approved" }, "充足")),
      r.description ? h("p", {}, r.description) : null,
      h("div", { class: "formula-box" }, r.expression),
      r.violation ? h("div", { class: "alert error small" }, r.violation.message) : null,
      h("div", { class: "row", style: { marginTop: "10px" } },
        h("button", { class: "small", onclick: () => trace(r.id, "up") }, "関与するパラメータを強調"),
        h("a", { class: "btn small", href: `#/s/${sid}/rules?key=${encodeURIComponent(r.key)}` }, "ルールを編集")),
    );
  }

  function renderSummary() {
    const nodes = graph.nodes;
    const count = (f) => nodes.filter(f).length;
    const stale = nodes.filter((n) => n.flag_stale);
    fill(side, 
      h("h2", {}, graph.system.name),
      graph.system.description ? h("p", { class: "muted small" }, graph.system.description) : null,
      h("dl", { class: "kv" },
        h("dt", {}, "パラメータ"), h("dd", {}, `${nodes.length} 件`),
        h("dt", {}, "承認済み"), h("dd", {}, `${count((n) => n.status === "approved")} 件`),
        h("dt", {}, "レビュー中"), h("dd", {}, `${count((n) => n.status === "in_review")} 件`),
        h("dt", {}, "要再計算"), h("dd", {}, `${stale.length} 件`),
        h("dt", {}, "制約違反"), h("dd", {}, `error ${graph.violations.filter((v) => v.severity === "error").length} / warning ${graph.violations.filter((v) => v.severity === "warning").length}`)),
      Object.keys(graph.rule_errors).length
        ? h("div", { class: "alert error small" }, "誤りのあるルールがあります",
          h("ul", {}, Object.entries(graph.rule_errors).map(([k, v]) => h("li", {}, `${k}: ${v.join("; ")}`)))) : null,
      stale.length
        ? h("section", {}, h("h3", {}, "要再計算のパラメータ"),
          h("ul", {}, stale.map((n) => h("li", {}, h("button", { class: "link", onclick: () => focus(n.ref) }, n.name),
            n.has_proposal ? h("span", { class: "small" }, ` ${fmtValue(n.value) || "∅"} → ${fmtValue(n.proposed_value)}`) : null))))
        : null,
      h("section", {}, h("h3", {}, "操作のヒント"),
        h("ul", { class: "small muted" },
          h("li", {}, "ノードをクリックすると詳細を表示します。Shift + クリックで上流・下流をたどります。"),
          h("li", {}, "ノードはドラッグで移動できます（位置はこのブラウザに保存）。"),
          h("li", {}, "背景のドラッグで移動、ホイールで拡大・縮小します。"))),
    );
  }

  function focus(ref) {
    canvas.center(ref);
    select(ref, "param");
  }

  function renderToolbar() {
    const stale = graph.nodes.filter((n) => n.flag_stale).length;
    const proposals = graph.nodes.filter((n) => n.has_proposal).length;
    const search = h("input", { type: "search", placeholder: "検索（名前・参照名）", value: canvas.filter.text, style: { width: "180px" } });
    search.addEventListener("input", () => { canvas.filter.text = search.value; canvas.draw(); });
    search.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const hit = graph.nodes.find((n) => canvas.matchesFilter(n));
      if (hit) focus(hit.ref);
    });
    const layerSel = h("select", {}, h("option", { value: "" }, "全レイヤー"), graph.layers.map((l) => h("option", { value: l.key, selected: canvas.filter.layer === l.key }, l.name)));
    layerSel.addEventListener("change", () => { canvas.filter.layer = layerSel.value; canvas.draw(); });

    const aiSel = h("select", { title: "AI 支援（対象: 全パラメータ）" }, h("option", { value: "" }, "AI 支援…"),
      Object.entries(PURPOSE_LABELS).map(([k, v]) => h("option", { value: k }, v)));
    aiSel.addEventListener("change", () => {
      const purpose = aiSel.value;
      aiSel.value = "";
      if (purpose) startAi(sid, purpose, [], onChange);
    });
    const exportSel = h("select", {}, h("option", { value: "" }, "書き出し…"),
      h("option", { value: "html" }, "設計書（HTML）"), h("option", { value: "csv" }, "パラメータシート（CSV）"),
      h("option", { value: "json" }, "全体（JSON）"), h("option", { value: "rules-json" }, "ルールのみ（JSON）"));
    exportSel.addEventListener("change", () => {
      const fmt = exportSel.value;
      exportSel.value = "";
      if (fmt) download(`/api/systems/${sid}/export?format=${fmt}`);
    });

    const diffToggle = h("label", { class: "row small", title: graph.baseline_changes ? "現行ベースラインから変更されたノードを破線で表示" : "承認済みの版がありません" },
      h("input", { type: "checkbox", checked: canvas.showChanges, disabled: !graph.baseline_changes, onchange: (e) => { canvas.showChanges = e.target.checked; applyChanges(); } }),
      "版との差分");
    applyChanges();

    fill(toolbar, 
      h("button", { class: "primary", onclick: () => paramDialog(sid, graph.layers, null, onChange) }, "＋ パラメータ"),
      h("span", { class: "sep" }),
      h("button", { onclick: recalcStale, disabled: !stale, title: "要再計算のパラメータとその下流の再計算案を作成します" }, `⟳ 再計算${stale ? `（${stale}）` : ""}`),
      h("button", { onclick: reviewProposals, disabled: !proposals }, `再計算案を適用${proposals ? `（${proposals}）` : ""}`),
      h("span", { class: "sep" }),
      search, layerSel, diffToggle,
      h("span", { class: "spacer" }),
      aiSel, exportSel,
      h("button", { class: "small", title: "ドラッグした位置を初期配置に戻します", onclick: () => { canvas.resetLayout(); canvas.fit(); } }, "配置をリセット"),
    );
  }

  function applyChanges() {
    canvas.changed = new Set(canvas.showChanges ? graph.baseline_changes || [] : []);
    canvas.draw();
  }

  function renderViolations() {
    const v = graph.violations;
    if (!v.length) { fill(violationsBox, h("span", { class: "muted" }, "✓ 制約違反はありません")); return; }
    fill(violationsBox, 
      h("div", { class: "small muted" }, `制約チェック: ${v.length} 件`),
      v.map((x) => h("div", { class: "v", onclick: () => { trace(`rule:${x.rule_key}`, "up"); select(`rule:${x.rule_key}`, "rule"); } },
        h("span", { class: `badge ${x.severity}` }, x.severity), " ", h("b", {}, x.rule_key), " ", x.message)));
  }

  async function recalcStale() {
    try {
      const res = await api.post(`/api/systems/${sid}/recalc`, { mode: "stale" });
      const proposed = res.filter((r) => r.status === "proposed").length;
      const errors = res.filter((r) => r.status === "error");
      toast(`再計算しました: 変更案 ${proposed} 件 / 変更なし ${res.filter((r) => r.status === "unchanged").length} 件${errors.length ? ` / エラー ${errors.length} 件` : ""}`, errors.length ? "error" : "info");
      await onChange();
      if (proposed) reviewProposals();
    } catch (e) { showError(e); }
  }

  function reviewProposals() {
    const items = graph.nodes.filter((n) => n.has_proposal);
    const checks = new Map(items.map((n) => [n.ref, h("input", { type: "checkbox", checked: true })]));
    modal({
      title: "再計算案の適用",
      wide: true,
      body: [
        h("p", { class: "hint" }, "適用した値は「算出式」として記録されます。承認済み・レビュー中のパラメータは下書きに戻ります。"),
        h("table", { class: "tbl" },
          h("tr", {}, ["", "パラメータ", "現在の値", "", "再計算案", "状態"].map((x) => h("th", {}, x))),
          items.map((n) => h("tr", {}, h("td", {}, checks.get(n.ref)), h("td", {}, n.name, h("div", { class: "mono small muted" }, n.ref)),
            h("td", { class: "num" }, fmtValue(n.value) || "∅"), h("td", {}, "→"), h("td", { class: "num" }, h("b", {}, fmtValue(n.proposed_value)), " ", n.unit),
            h("td", {}, n.status !== "draft" ? h("span", { class: "badge upstream" }, "下書きに戻る") : null)))),
      ],
      buttons: [{ label: "キャンセル" }, {
        label: "選択した案を適用", primary: true,
        onClick: async () => {
          const refs = [...checks].filter(([, c]) => c.checked).map(([r]) => r);
          if (!refs.length) return true;
          const applied = await api.post(`/api/systems/${sid}/apply-proposals`, { refs });
          toast(`${applied.length} 件の再計算案を適用しました`);
          await onChange();
        },
      }],
    });
  }

  await reload();
  if (selected) canvas.center(selected);
  const onResize = () => canvas.applyTransform();
  window.addEventListener("resize", onResize);
  return () => window.removeEventListener("resize", onResize);
}

function legend() {
  const item = (color, label) => h("span", {}, h("i", { style: { background: color } }), label);
  return h("div", { class: "legend" },
    item("var(--draft)", "下書き"), item("var(--aieval)", "AI評価済み"), item("var(--review)", "レビュー中"), item("var(--ok)", "承認済み"),
    item("var(--stale)", "⟳ 要再計算"), item("var(--upstream)", "▲ 承認後に上流変更"), item("var(--danger)", "! 制約違反"),
    h("span", { class: "muted" }, "実線=算出式 / 破線=制約 / 点線=関連"));
}
