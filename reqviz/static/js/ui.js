// DOM ヘルパー、モーダル、トースト、表示用フォーマッタ

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked" || k === "disabled" || k === "selected") el[k] = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}

function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

const SVG_NS = "http://www.w3.org/2000/svg";
export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v);
  }
  append(el, children);
  return el;
}

export function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); return el; }

export function toast(message, kind = "info", ms = 3500) {
  const box = document.getElementById("toasts");
  const t = h("div", { class: `toast ${kind}` }, message);
  box.append(t);
  setTimeout(() => t.remove(), kind === "error" ? ms * 2 : ms);
}

/** モーダルを開く。buttons: [{label, primary, danger, onClick: async () => 閉じるなら true}] */
export function modal({ title, body, buttons = [], wide = false, onClose }) {
  const backdrop = h("div", { class: "modal-backdrop" });
  const close = () => { backdrop.remove(); document.removeEventListener("keydown", onKey); onClose && onClose(); };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  const footer = h("footer");
  const btns = buttons.length ? buttons : [{ label: "閉じる" }];
  for (const b of btns) {
    const btn = h("button", { class: b.primary ? "primary" : b.danger ? "danger" : "" }, b.label);
    btn.addEventListener("click", async () => {
      if (!b.onClick) return close();
      btn.disabled = true;
      try { if ((await b.onClick()) !== false) close(); }
      catch (e) { showError(e); }
      finally { btn.disabled = false; }
    });
    footer.append(btn);
  }
  const m = h("div", { class: `modal ${wide ? "wide" : ""}`, role: "dialog", "aria-modal": "true" },
    h("header", {}, title), h("div", { class: "body" }, body), footer);
  backdrop.append(m);
  backdrop.addEventListener("mousedown", (e) => { if (e.target === backdrop) close(); });
  document.addEventListener("keydown", onKey);
  document.body.append(backdrop);
  const first = m.querySelector("input, textarea, select");
  if (first) first.focus();
  return { close, el: m };
}

/** コメント入力付きの確認ダイアログ。キャンセル時 null。 */
export function promptComment({ title, message = "", required = false, placeholder = "コメント", okLabel = "OK", danger = false }) {
  return new Promise((resolve) => {
    let done = false;
    const ta = h("textarea", { placeholder });
    const err = h("div", { class: "err small" });
    modal({
      title,
      body: [message ? h("p", {}, message) : null, ta, err],
      buttons: [
        { label: "キャンセル" },
        {
          label: okLabel, primary: !danger, danger,
          onClick: () => {
            if (required && !ta.value.trim()) { err.textContent = "コメントを入力してください"; return false; }
            done = true; resolve(ta.value.trim()); return true;
          },
        },
      ],
      onClose: () => { if (!done) resolve(null); },
    });
  });
}

export function confirmDialog(title, message, okLabel = "OK", danger = false) {
  return new Promise((resolve) => {
    let done = false;
    modal({
      title, body: h("p", {}, message),
      buttons: [{ label: "キャンセル" }, { label: okLabel, danger, primary: !danger, onClick: () => { done = true; resolve(true); } }],
      onClose: () => { if (!done) resolve(false); },
    });
  });
}

/** API エラーを人が読める形で表示する */
export function showError(e) {
  console.error(e);
  const d = e && e.details;
  let msg = (e && e.message) || String(e);
  const list = d && (d.reasons || d.errors || (d.rules ? d.rules.map((r) => `参照しているルール: ${r}`) : null));
  if (Array.isArray(list) && list.length) msg += "\n・" + list.join("\n・");
  if (d && d.rule_errors) msg += "\n" + Object.entries(d.rule_errors).map(([k, v]) => `・${k}: ${v.join("; ")}`).join("\n");
  toast(msg, "error");
}

export function fmtValue(v) {
  if (v === null || v === undefined || v === "") return "";
  if (typeof v === "boolean") return v ? "true" : "false";
  if (typeof v === "number") return Number.isInteger(v) ? v.toLocaleString("ja-JP") : String(+v.toPrecision(10));
  return String(v);
}

export function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}/${p(d.getMonth() + 1)}/${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export const STATUS_LABELS = { draft: "下書き", ai_evaluated: "AI評価済み", in_review: "レビュー中", approved: "承認済み" };
export const KIND_LABELS = { formula: "算出式", constraint: "制約", nl: "自然言語" };
export const SOURCE_LABELS = { manual: "手入力", formula: "算出式", ai: "AI提案" };
export const TYPE_LABELS = { number: "数値", string: "文字列", enum: "選択肢", bool: "真偽値" };
export const BASELINE_LABELS = { in_review: "レビュー中", approved: "承認済み", rejected: "差し戻し" };

export function statusBadge(status) { return h("span", { class: `badge ${status}` }, STATUS_LABELS[status] || status); }

export function flagBadges(p) {
  return [
    p.flag_stale ? h("span", { class: "badge stale", title: "上流の値またはルールが変わりました。再計算してください" }, "要再計算") : null,
    p.flag_upstream_changed ? h("span", { class: "badge upstream", title: "承認後に上流が変更されています" }, "承認後に上流変更") : null,
    p.violation ? h("span", { class: `badge ${p.violation}` }, p.violation === "error" ? "制約違反" : "警告") : null,
    p.ai_skipped ? h("span", { class: "badge", title: "AI 評価をスキップしました" }, "AIスキップ") : null,
  ];
}

/** 値の入力欄（型に応じて input / select） */
export function valueInput(p, value) {
  if (p.type === "enum") {
    return h("select", {}, h("option", { value: "" }, "（未設定）"),
      p.enum_options.map((o) => h("option", { value: o, selected: value === o }, o)));
  }
  if (p.type === "bool") {
    return h("select", {}, h("option", { value: "" }, "（未設定）"),
      h("option", { value: "true", selected: value === true }, "true"),
      h("option", { value: "false", selected: value === false }, "false"));
  }
  return h("input", { type: p.type === "number" ? "text" : "text", inputmode: p.type === "number" ? "decimal" : undefined, value: value ?? "" });
}

export function readValue(p, el) {
  const raw = el.value;
  if (raw === "") return null;
  if (p.type === "bool") return raw === "true";
  if (p.type === "number") {
    const n = Number(raw.replace(/,/g, ""));
    return Number.isFinite(n) ? n : raw; // 数値でなければそのまま送り、サーバー側で検証エラーにする
  }
  return raw;
}

export function download(url) {
  const a = h("a", { href: url, download: "" });
  document.body.append(a); a.click(); a.remove();
}

export function readJsonFile() {
  return new Promise((resolve, reject) => {
    const input = h("input", { type: "file", accept: ".json,application/json" });
    input.addEventListener("change", async () => {
      const f = input.files[0];
      if (!f) return resolve(null);
      try { resolve(JSON.parse(await f.text())); } catch (e) { reject(new Error("JSON ファイルを読み込めません: " + e.message)); }
    });
    input.click();
  });
}

/** 子要素を置き換える（配列・null を許容） */
export function fill(el, ...children) {
  clear(el);
  append(el, children);
  return el;
}
