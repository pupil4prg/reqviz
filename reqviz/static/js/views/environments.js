// 環境（本番・検証・開発など）: 追加、環境間の比較、構成の同期、値のコピー

import { api } from "../api.js";
import { navigate, refreshNav } from "../main.js";
import { confirmDialog, fmtAny, fmtTime, h, modal, showError, toast, fill } from "../ui.js";

const STATUS = { draft: "下書き", ai_evaluated: "AI評価済み", in_review: "レビュー中", approved: "承認済み" };
const CHANGE = { added: "追加", removed: "削除", modified: "変更" };
const SECTION = { layers: "レイヤー", components: "コンポーネント", component_deps: "コンポーネントの依存", params: "パラメータ", items: "構成項目", rules: "ルール", relations: "関連" };
const DEF_FIELDS = { name: "名前", type: "型", enum_options: "選択肢", unit: "単位", aliases: "別名", component: "コンポーネント", kind: "種別", options: "候補", content_type: "形式" };

export async function render(root, { sid }) {
  const page = h("div", { class: "page" });
  root.append(page);
  let onlyDiff = true;

  async function load() {
    const envs = await api.get(`/api/systems/${sid}/environments`);
    refreshNav(sid);
    if (envs.length === 1 && !envs[0].env_group_id) return renderIntro(envs[0]);
    const cmp = await api.get(`/api/systems/${sid}/environments/compare`);
    renderMain(envs, cmp);
  }

  const keyInput = (ph) => h("input", { placeholder: ph, autocapitalize: "none" });

  function renderIntro() {
    const curKey = keyInput("例: prod"), curName = h("input", { placeholder: "例: 本番" });
    const newKey = keyInput("例: dev"), newName = h("input", { placeholder: "例: 開発" });
    fill(page,
      h("h1", {}, "環境"),
      h("p", { class: "hint" }, "本番・検証・開発のように、環境ごとに値が異なる構成を管理します。環境を追加すると、このシステムの構成と値をコピーした環境ができ、",
        "値・承認・版・履歴は環境ごとに独立します。環境の間の違いは、この画面で比較でき、構成（パラメータの定義やルール）はまとめて同期できます。"),
      h("div", { class: "card", style: { maxWidth: "560px" } },
        h("h3", {}, "環境を分ける"),
        h("p", { class: "small muted" }, "まず、今のシステムがどの環境かを決め、追加する環境の名前を入力します。"),
        h("div", { class: "grid2" }, h("label", { class: "field" }, "今のシステムの環境キー", curKey), h("label", { class: "field" }, "環境名", curName)),
        h("div", { class: "grid2" }, h("label", { class: "field" }, "追加する環境のキー", newKey), h("label", { class: "field" }, "環境名", newName)),
        h("button", {
          class: "primary",
          onclick: async () => {
            try {
              await api.post(`/api/systems/${sid}/environments`, { key: newKey.value.trim(), name: newName.value, current_key: curKey.value.trim(), current_name: curName.value });
              toast("環境を追加しました");
              load();
            } catch (e) { showError(e); }
          },
        }, "環境を追加")));
  }

  function renderMain(envs, cmp) {
    const cur = envs.find((e) => e.is_current);
    const others = envs.filter((e) => !e.is_current);
    const order = [cur, ...others];
    const selected = new Set();
    const copyBtn = h("button", { disabled: true, onclick: () => copyDialog(cur, others, [...selected]) }, "選択した値をコピー…");
    const check = (ref) => h("input", {
      type: "checkbox", title: "値のコピー対象にする",
      onchange: (e) => { e.target.checked ? selected.add(ref) : selected.delete(ref); copyBtn.disabled = !selected.size; copyBtn.textContent = selected.size ? `選択した値をコピー…（${selected.size}）` : "選択した値をコピー…"; },
    });

    const cell = (row, env) => {
      const v = row.values[String(env.id)];
      if (!v) return h("td", { class: "env-missing" }, "（なし）");
      return h("td", { class: row.differs ? "env-diff" : "" }, fmtAny(v.value, 60) || h("span", { class: "muted" }, "未設定"),
        row.unit ? h("span", { class: "muted small" }, ` ${row.unit}`) : null,
        h("div", { class: "small muted" }, STATUS[v.status] || v.status));
    };
    const notes = (row) => [
      row.missing_in.length ? h("span", { class: "badge warning" }, "ない環境あり") : null,
      row.definition_differs.length ? h("span", { class: "badge warning", title: "構成の同期でそろえられます" }, `定義が異なる: ${row.definition_differs.map((f) => DEF_FIELDS[f] || f).join("・")}`) : null,
    ];
    const visible = (row) => !onlyDiff || row.differs || row.missing_in.length || row.definition_differs.length;
    const table = (rows, label) => {
      const shown = rows.filter(visible);
      return [h("h3", {}, `${label}（${shown.length} / ${rows.length}）`),
        shown.length ? h("div", { class: "table-wrap" }, h("table", { class: "tbl env-table" },
          h("tr", {}, h("th", {}, ""), h("th", {}, "項目"), order.map((e) => h("th", {}, e.env_name, e.is_current ? h("span", { class: "muted small" }, "（この環境）") : null)), h("th", {}, "")),
          shown.map((row) => h("tr", {},
            h("td", {}, row.values[String(cur.id)] ? check(row.ref) : null),
            h("td", {}, row.name, h("div", { class: "mono small muted" }, row.ref)),
            order.map((e) => cell(row, e)),
            h("td", {}, h("div", { class: "row" }, notes(row)))))))
          : h("p", { class: "muted small" }, onlyDiff ? "違いはありません" : "ありません")];
    };

    fill(page,
      h("div", { class: "row" }, h("h1", {}, "環境"), h("span", { class: "spacer" }),
        h("button", { class: "primary", onclick: () => addDialog(cur) }, "＋ 環境を追加")),
      h("p", { class: "hint" }, "値・承認・版・履歴は環境ごとに独立しています。上部の環境名から切り替えられます。"),
      h("table", { class: "tbl" },
        h("tr", {}, ["環境", "キー", "パラメータ", "承認済み", "現行の版", "更新", ""].map((x) => h("th", {}, x))),
        envs.map((e) => h("tr", {},
          h("td", {}, e.is_current ? h("b", {}, e.env_name) : h("a", { href: `#/s/${e.id}/environments` }, e.env_name), e.is_current ? h("span", { class: "badge" }, "この環境") : null),
          h("td", {}, h("code", {}, e.env_key)), h("td", { class: "num" }, e.param_count), h("td", { class: "num" }, e.approved_count),
          h("td", {}, e.current_baseline_version ? `v${e.current_baseline_version}` : h("span", { class: "muted" }, "なし")),
          h("td", { class: "small" }, fmtTime(e.updated_at)),
          h("td", {}, h("div", { class: "row" },
            h("button", { class: "small", onclick: () => renameDialog(e) }, "名前を変更"),
            h("button", { class: "small", onclick: () => detach(e) }, "グループから外す")))))),

      h("div", { class: "row", style: { marginTop: "22px" } }, h("h2", { style: { margin: 0 } }, "環境の比較"),
        h("span", { class: "small muted" }, `値の違い ${cmp.summary.value_differences} 件 ／ 構成の違い ${cmp.summary.structure_differences} 件`),
        h("span", { class: "spacer" }),
        h("label", { class: "row small" }, h("input", { type: "checkbox", checked: onlyDiff, onchange: (e) => { onlyDiff = e.target.checked; renderMain(envs, cmp); } }), "違いのあるものだけ"),
        copyBtn,
        h("button", { onclick: () => syncDialog(cur, others) }, "構成を同期…")),
      h("p", { class: "hint" }, `「構成を同期」は、${cur.env_name}の定義（パラメータの名前・型・単位・別名、選択肢の候補、ルールなど）を他の環境にそろえます。値は各環境のものを保ちます。`,
        "「選択した値をコピー」は、チェックした項目の値を他の環境に写します。"),
      table(cmp.params, "パラメータ"),
      cmp.items.length ? table(cmp.items, "構成項目") : null,
      h("h3", {}, `ルールの違い（${cmp.rules.length}）`),
      cmp.rules.length ? h("table", { class: "tbl" },
        h("tr", {}, h("th", {}, "ルール"), order.map((e) => h("th", {}, e.env_name))),
        cmp.rules.map((r) => h("tr", {}, h("td", {}, h("code", {}, r.key)),
          order.map((e) => { const b = r.rules[String(e.id)]; return b ? h("td", { class: "env-diff small mono" }, b.expression || b.nl_text || "", b.enabled ? "" : "（無効）", ` [${b.severity}]`) : h("td", { class: "env-missing" }, "（なし）"); }))))
        : h("p", { class: "muted small" }, "ルールはすべての環境で同じです"),
    );
  }

  function addDialog(cur) {
    const key = keyInput("例: stg"), name = h("input", { placeholder: "例: 検証" });
    modal({
      title: "環境を追加",
      body: [h("p", { class: "small muted" }, `${cur.env_name}の構成と値をコピーして、新しい環境を作ります。承認は新しい環境でやり直します。`),
        h("div", { class: "grid2" }, h("label", { class: "field" }, "環境キー", key), h("label", { class: "field" }, "環境名", name))],
      buttons: [{ label: "キャンセル" }, {
        label: "追加", primary: true,
        onClick: async () => {
          const s = await api.post(`/api/systems/${sid}/environments`, { key: key.value.trim(), name: name.value });
          toast("環境を追加しました");
          navigate(`#/s/${s.id}/environments`);
        },
      }],
    });
  }

  function renameDialog(e) {
    const key = h("input", { value: e.env_key }), name = h("input", { value: e.env_name });
    modal({
      title: "環境の名前を変更",
      body: h("div", { class: "grid2" }, h("label", { class: "field" }, "環境キー", key), h("label", { class: "field" }, "環境名", name)),
      buttons: [{ label: "キャンセル" }, { label: "保存", primary: true, onClick: async () => { await api.put(`/api/systems/${e.id}/environment`, { key: key.value.trim(), name: name.value }); load(); } }],
    });
  }

  async function detach(e) {
    if (!(await confirmDialog("グループから外す", `${e.env_name}を環境のグループから外し、独立したシステムにします。データは消えません（削除はシステム一覧から行います）。`, "外す"))) return;
    try { await api.del(`/api/systems/${e.id}/environment`); toast("グループから外しました"); load(); } catch (err) { showError(err); }
  }

  function syncDialog(cur, others) {
    const checks = others.map((e) => [e, h("input", { type: "checkbox", checked: true })]);
    const strict = h("input", { type: "checkbox" });
    const result = h("div");
    let previewed = false;
    const body = () => ({ targets: checks.filter(([, c]) => c.checked).map(([e]) => e.id), delete_extra: strict.checked });
    const reset = () => { previewed = false; fill(result); };
    [...checks.map(([, c]) => c), strict].forEach((c) => c.addEventListener("change", reset));
    modal({
      title: `構成を同期（${cur.env_name} → 他の環境）`, wide: true,
      body: [
        h("p", { class: "small" }, `${cur.env_name}の定義とルールを、選んだ環境に反映します。値・採用案・本文・根拠・タグは反映先のものを保ち、値が変わらない項目の承認も保ちます。`),
        h("div", { class: "row" }, h("span", { class: "small muted" }, "反映先:"), checks.map(([e, c]) => h("label", { class: "row" }, c, e.env_name))),
        h("label", { class: "row small", style: { marginTop: "6px" } }, strict, "反映先にだけある項目・ルールを削除する（既定では残します）"),
        result],
      buttons: [{ label: "キャンセル" }, {
        label: "差分を確認", onClick: async () => {
          const res = await api.post(`/api/systems/${sid}/environments/sync`, { ...body(), preview: true });
          previewed = true;
          fill(result, res.targets.map((t) => [
            h("h3", {}, `${t.env_name}: ${t.changes} 件の変更`),
            t.changes ? h("table", { class: "tbl" }, Object.entries(t.diff).flatMap(([sec, list]) => list.map((x) => h("tr", {},
              h("td", { class: "small muted" }, SECTION[sec]), h("td", {}, h("span", { class: `diff-${x.change}` }, CHANGE[x.change])), h("td", {}, h("code", {}, x.key)),
              h("td", { class: "small" }, x.fields ? x.fields.map((f) => h("div", {}, h("b", {}, f.field), ": ", fmtAny(f.before, 50) || "∅", " → ", fmtAny(f.after, 50) || "∅")) : ""))))) : h("p", { class: "muted small" }, "差分はありません"),
          ]));
          return false;
        },
      }, {
        label: "同期する", primary: true, onClick: async () => {
          if (!previewed) { toast("先に「差分を確認」で内容を確認してください", "error"); return false; }
          const res = await api.post(`/api/systems/${sid}/environments/sync`, body());
          toast(`${res.targets.filter((t) => t.changes).length} 環境に同期しました`);
          load();
        },
      }],
    });
  }

  function copyDialog(cur, others, refs) {
    const sel = h("select", {}, others.map((e) => h("option", { value: e.id }, e.env_name)));
    const comment = h("input", { placeholder: "変更理由（履歴に残ります・任意）" });
    modal({
      title: `値をコピー（${refs.length} 件）`,
      body: [h("p", { class: "small" }, `${cur.env_name}の値・採用案・本文を、選んだ環境に写します。反映先では通常の変更として扱い、値が変わった項目は下書きに戻ります。算出式で決まる値は対象外です。`),
        h("label", { class: "field" }, "反映先", sel), h("label", { class: "field" }, "変更理由", comment)],
      buttons: [{ label: "キャンセル" }, {
        label: "コピー", primary: true,
        onClick: async () => {
          const res = await api.post(`/api/systems/${sid}/environments/copy-values`, { target: Number(sel.value), refs, comment: comment.value });
          toast(`${res.copied.length} 件をコピーしました${res.skipped.length ? `（${res.skipped.length} 件は対象外）` : ""}`);
          if (res.skipped.length) modal({ title: "コピーしなかった項目", body: h("ul", {}, res.skipped.map((x) => h("li", {}, h("code", {}, x.ref), " ", x.reason))) });
          load();
        },
      }],
    });
  }

  await load();
}
