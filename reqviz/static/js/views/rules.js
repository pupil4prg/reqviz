// ルール一覧とエディタ（入力中に検証）

import { api } from "../api.js";
import { confirmDialog, download, h, KIND_LABELS, modal, readJsonFile, showError, toast, fill } from "../ui.js";

const FUNCS = "ceil, floor, round, min, max, abs, sum";

export async function render(root, { sid, query }) {
  const page = h("div", { class: "page" });
  root.append(page);
  let kindFilter = "";

  async function load() {
    const [rules, params] = await Promise.all([api.get(`/api/systems/${sid}/rules`), api.get(`/api/systems/${sid}/params`)]);
    const shown = rules.filter((r) => !kindFilter || r.kind === kindFilter);
    const kindSel = h("select", {}, h("option", { value: "" }, "すべての種別"),
      Object.entries(KIND_LABELS).map(([k, v]) => h("option", { value: k, selected: kindFilter === k }, v)));
    kindSel.addEventListener("change", () => { kindFilter = kindSel.value; load(); });

    fill(page, 
      h("div", { class: "row" }, h("h1", {}, "ルール"), h("span", { class: "spacer" }),
        h("button", { onclick: importRules }, "ルール JSON を取り込み"),
        h("button", { onclick: () => download(`/api/systems/${sid}/export?format=rules-json`) }, "ルール JSON を書き出し"),
        h("button", { class: "primary", onclick: () => editor(null, params, rules) }, "＋ ルール")),
      h("p", { class: "hint" },
        "算出式・制約は Python の式構文で書きます。パラメータは「レイヤー.キー」で参照し、使える関数は ", h("code", {}, FUNCS),
        "、条件分岐は ", h("code", {}, "0.5 if biz.business_type == 'online' else 0.2"), " のように書きます。"),
      h("div", { class: "row", style: { marginBottom: "8px" } }, kindSel),
      h("table", { class: "tbl" },
        h("tr", {}, ["キー", "種別", "対象", "内容", "重要度", "説明", ""].map((x) => h("th", {}, x))),
        shown.map((r) => h("tr", { style: r.enabled ? {} : { opacity: 0.55 } },
          h("td", {}, h("b", {}, r.key), r.enabled ? null : h("div", { class: "small muted" }, "無効")),
          h("td", {}, KIND_LABELS[r.kind]),
          h("td", {}, r.target ? h("code", {}, r.target) : ""),
          h("td", {}, h("div", { class: r.kind === "nl" ? "" : "mono" }, r.expression || r.nl_text),
            r.errors.length ? h("div", { class: "err small" }, r.errors.join(" / ")) : null),
          h("td", {}, r.kind === "constraint" ? h("span", { class: `badge ${r.severity}` }, r.severity) : ""),
          h("td", { class: "small muted" }, r.description),
          h("td", {}, h("div", { class: "row" },
            h("button", { class: "small", onclick: () => editor(r, params, rules) }, "編集"),
            h("button", { class: "small danger", onclick: () => remove(r) }, "削除")))))),
    );
    if (query.key) {
      const r = rules.find((x) => x.key === query.key);
      query.key = null;
      if (r) editor(r, params, rules);
    }
  }

  function editor(rule, params, rules) {
    const datalist = h("datalist", { id: "param-refs" }, params.map((p) => h("option", { value: p.ref }, p.name)));
    const key = h("input", { value: rule ? rule.key : suggestKey(rules), placeholder: "例: F-010" });
    const kind = h("select", {}, Object.entries(KIND_LABELS).map(([k, v]) => h("option", { value: k, selected: rule && rule.kind === k }, v)));
    const target = h("input", { list: "param-refs", value: rule ? rule.target || "" : "", placeholder: "layer.key" });
    const expr = h("textarea", { class: "mono", style: { minHeight: "60px" } }, rule ? rule.expression || "" : "");
    const nl = h("textarea", { placeholder: "AI に渡す判断基準を文章で書きます" }, rule ? rule.nl_text || "" : "");
    const severity = h("select", {}, ["error", "warning"].map((s) => h("option", { value: s, selected: rule && rule.severity === s }, s)));
    const desc = h("input", { value: rule ? rule.description : "" });
    const enabled = h("input", { type: "checkbox", checked: rule ? rule.enabled : true });
    const status = h("div", { class: "small", style: { minHeight: "20px" } });

    const fTarget = h("label", { class: "field" }, "対象パラメータ", target);
    const fExpr = h("label", { class: "field" }, "式", expr);
    const fNl = h("label", { class: "field" }, "ルール本文", nl);
    const fSeverity = h("label", { class: "field" }, "重要度", severity);

    const insertRef = h("select", { class: "small" }, h("option", { value: "" }, "参照を挿入…"), params.map((p) => h("option", { value: p.ref }, `${p.ref}（${p.name}）`)));
    insertRef.addEventListener("change", () => {
      const v = insertRef.value;
      insertRef.value = "";
      if (!v) return;
      const at = expr.selectionStart ?? expr.value.length;
      expr.value = expr.value.slice(0, at) + v + expr.value.slice(expr.selectionEnd ?? at);
      expr.focus();
      validate();
    });

    function sync() {
      const k = kind.value;
      fTarget.style.display = k === "constraint" ? "none" : "";
      fExpr.style.display = k === "nl" ? "none" : "";
      insertRef.style.display = k === "nl" ? "none" : "";
      fNl.style.display = k === "nl" ? "" : "none";
      fSeverity.style.display = k === "formula" ? "none" : "";
      target.placeholder = k === "nl" ? "（任意）layer.key" : "代入先 layer.key";
    }

    function body() {
      return {
        id: rule ? rule.id : undefined, key: key.value.trim(), kind: kind.value, target: target.value.trim() || null,
        expression: expr.value.trim() || null, nl_text: nl.value.trim() || null, severity: severity.value,
        description: desc.value, enabled: enabled.checked,
      };
    }

    let timer = null;
    function validate() {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        try {
          const res = await api.post(`/api/systems/${sid}/rules/validate`, body());
          fill(status, res.ok ? h("span", { style: { color: "var(--ok)" } }, "✓ 問題ありません")
            : h("span", { class: "err" }, res.errors.join(" / ")));
        } catch (e) { status.textContent = e.message; }
      }, 300);
    }
    for (const el of [key, kind, target, expr, nl, enabled]) el.addEventListener("input", validate);
    kind.addEventListener("change", () => { sync(); validate(); });
    sync();
    validate();

    modal({
      title: rule ? `ルール ${rule.key} を編集` : "ルールを追加",
      wide: true,
      body: [
        datalist,
        h("div", { class: "grid2" }, h("label", { class: "field" }, "キー", key), h("label", { class: "field" }, "種別", kind)),
        fTarget, fExpr, h("div", { class: "row" }, insertRef, h("span", { class: "hint" }, `関数: ${FUNCS}`)), fNl,
        h("div", { class: "grid2" }, fSeverity, h("label", { class: "field" }, "説明", desc)),
        h("label", { class: "row small" }, enabled, "有効"),
        status,
        h("p", { class: "hint" }, "算出式を変更すると、代入先と下流のパラメータが「要再計算」になります。"),
      ],
      buttons: [{ label: "キャンセル" }, {
        label: "保存", primary: true,
        onClick: async () => {
          const b = body();
          if (rule) await api.put(`/api/rules/${rule.id}`, { ...b, row_version: rule.row_version });
          else await api.post(`/api/systems/${sid}/rules`, b);
          toast("ルールを保存しました");
          load();
        },
      }],
    });
  }

  async function remove(r) {
    if (!(await confirmDialog("ルールを削除", `ルール ${r.key} を削除します。`, "削除", true))) return;
    try { await api.del(`/api/rules/${r.id}`); toast("削除しました"); load(); } catch (e) { showError(e); }
  }

  async function importRules() {
    try {
      const payload = await readJsonFile();
      if (!payload) return;
      const res = await api.post(`/api/systems/${sid}/import`, { payload });
      toast(`ルールを取り込みました（追加 ${res.created.length} 件 / 更新 ${res.updated.length} 件）`);
      load();
    } catch (e) { showError(e); }
  }

  await load();
}

function suggestKey(rules) {
  const nums = rules.map((r) => Number((r.key.match(/(\d+)$/) || [])[1] || 0));
  const n = Math.max(0, ...nums) + 1;
  return `R-${String(n).padStart(3, "0")}`;
}
