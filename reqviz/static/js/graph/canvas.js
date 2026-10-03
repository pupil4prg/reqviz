// SVG によるグラフ描画（パン・ズーム・ドラッグ・強調表示）
//
// レイアウト（初期座標）はサーバーが計算する。ユーザーがドラッグした位置は
// localStorage に保存し、次回以降も使う。

import { fmtValue, s } from "../ui.js";

const NODE_W = 230;
const NODE_H = 64;
const RULE_INSET_X = 30;
const RULE_INSET_Y = 12;

const STATUS_COLOR = {
  draft: "var(--draft)", ai_evaluated: "var(--aieval)", in_review: "var(--review)", approved: "var(--ok)",
};

export function nodeColor(n) {
  if (n.violation === "error") return "var(--danger)";
  if (n.flag_stale) return "var(--stale)";
  if (n.flag_upstream_changed) return "var(--upstream)";
  return STATUS_COLOR[n.status] || "var(--draft)";
}

function trim(text, max) {
  text = String(text ?? "");
  let w = 0;
  let out = "";
  for (const ch of text) {
    w += /[\u0000-ÿ]/.test(ch) ? 1 : 2;
    if (w > max) return out + "…";
    out += ch;
  }
  return out;
}

export class GraphCanvas {
  constructor(container, { onSelect, storageKey }) {
    this.container = container;
    this.onSelect = onSelect;
    this.storageKey = storageKey;
    this.view = { x: 0, y: 0, k: 1 };
    this.saved = this.loadSaved();
    this.highlight = null; // Set of ids or null
    this.selected = null;
    this.changed = new Set();
    this.filter = { text: "", layer: "" };

    this.svg = s("svg", { role: "img", "aria-label": "パラメータ関連図" });
    this.defs = s("defs", {},
      marker("arr", "var(--edge)"), marker("arr-hl", "var(--highlight)"), marker("arr-bad", "var(--danger)"));
    this.root = s("g");
    this.svg.append(this.defs, this.root);
    container.append(this.svg);
    this.bindPanZoom();
  }

  loadSaved() {
    try { return JSON.parse(localStorage.getItem(this.storageKey) || "{}"); } catch { return {}; }
  }

  persist() {
    try { localStorage.setItem(this.storageKey, JSON.stringify(this.saved)); } catch { /* 保存できなくても動作は継続 */ }
  }

  resetLayout() {
    this.saved = {};
    this.persist();
    this.draw();
  }

  setData(graph) {
    const first = !this.graph;
    this.graph = graph;
    this.changed = new Set(graph.baseline_changes || []);
    this.draw();
    if (first) this.fit();
  }

  pos(id, fallback) {
    const p = this.saved[id];
    return p ? { x: p[0], y: p[1] } : fallback;
  }

  boxes() {
    const g = this.graph;
    const map = new Map();
    for (const n of g.nodes) {
      const p = this.pos(n.ref, { x: n.x, y: n.y });
      map.set(n.ref, { x: p.x, y: p.y, w: NODE_W, h: NODE_H });
    }
    for (const r of g.rule_nodes) {
      const p = this.pos(r.id, { x: r.x, y: r.y });
      map.set(r.id, { x: p.x + RULE_INSET_X, y: p.y + RULE_INSET_Y, w: NODE_W - RULE_INSET_X * 2, h: NODE_H - RULE_INSET_Y * 2, ox: p.x, oy: p.y });
    }
    return map;
  }

  matchesFilter(n) {
    const t = this.filter.text.trim().toLowerCase();
    if (this.filter.layer && n.layer_key !== this.filter.layer) return false;
    if (!t) return true;
    return [n.ref, n.name, n.key, n.description, n.expression].some((v) => v && String(v).toLowerCase().includes(t));
  }

  draw() {
    const g = this.graph;
    if (!g) return;
    const boxes = this.boxes();
    this.boxesCache = boxes;
    const root = this.root;
    root.replaceChildren();

    // レイヤーの見出しと帯
    const layerNames = Object.fromEntries(g.layers.map((l) => [l.key, l.name]));
    const height = Math.max(g.size.height, ...[...boxes.values()].map((b) => b.y + b.h + 40));
    for (const col of g.columns) {
      root.append(
        s("rect", { class: "layer-band", x: col.x - 16, y: 8, width: NODE_W + 32, height: height - 8, rx: 10 }),
        s("text", { class: "layer-label", x: col.x + NODE_W / 2, y: 32, "text-anchor": "middle" }, layerNames[col.layer_key] || col.layer_key),
      );
    }

    const hl = this.highlight;
    const dimmed = (id) => (hl && !hl.has(id)) || this.filteredOut.has(id);
    this.filteredOut = new Set([...g.nodes.map((n) => [n.ref, n]), ...g.rule_nodes.map((r) => [r.id, r])]
      .filter(([, n]) => !this.matchesFilter(n)).map(([id]) => id));
    const filtering = this.filter.text || this.filter.layer;
    if (!filtering) this.filteredOut.clear();

    // エッジ
    const ruleViolation = Object.fromEntries(g.rule_nodes.map((r) => [r.id, r.violation]));
    const edgeLayer = s("g");
    for (const e of g.edges) {
      const a = boxes.get(e.from);
      const b = boxes.get(e.to);
      if (!a || !b) continue;
      const isHl = hl && hl.has(e.from) && hl.has(e.to);
      const bad = e.kind === "constrains" && ruleViolation[e.to];
      const cls = ["edge", e.kind, isHl ? "hl" : "", bad ? "bad" : "", (hl && !isHl) || dimmed(e.from) || dimmed(e.to) ? "dim" : ""].join(" ");
      const path = s("path", { class: cls, d: edgePath(a, b), "marker-end": `url(#${isHl ? "arr-hl" : bad ? "arr-bad" : "arr"})` });
      const tip = e.kind === "derives" ? `算出式 ${e.rule_key}` : e.kind === "constrains" ? `制約 ${e.rule_key}` : `関連: ${e.note || ""}`;
      path.append(s("title", {}, `${e.from} → ${e.to}（${tip}）`));
      edgeLayer.append(path);
    }
    root.append(edgeLayer);

    // 制約ノード
    for (const r of g.rule_nodes) {
      const b = boxes.get(r.id);
      const color = r.violation ? (r.severity === "error" ? "var(--danger)" : "var(--warn)") : "var(--edge)";
      const el = s("g", { class: `rule-node ${dimmed(r.id) ? "dim" : ""}`, transform: `translate(${b.x},${b.y})`, "data-id": r.id },
        s("rect", { class: "box", width: b.w, height: b.h, rx: b.h / 2, stroke: color }),
        s("text", { x: b.w / 2, y: b.h / 2 + 4, "text-anchor": "middle" }, `${r.violation ? "⚠ " : ""}${r.key}: ${trim(r.description || r.expression, 16)}`),
        s("title", {}, `${r.key}（${r.severity}）\n${r.expression}\n${r.violation ? r.violation.message : "充足"}`));
      this.bindNode(el, r.id, "rule");
      root.append(el);
    }

    // パラメータノード
    for (const n of g.nodes) {
      const b = boxes.get(n.ref);
      const color = nodeColor(n);
      const value = fmtValue(n.value);
      const valText = (value === "" ? "（未設定）" : value) + (n.unit ? ` ${n.unit}` : "");
      const flags = (n.flag_stale ? "⟳" : "") + (n.flag_upstream_changed ? "▲" : "") + (n.violation ? "!" : "");
      const cls = ["node", this.selected === n.ref ? "selected" : "", hl && hl.has(n.ref) ? "hl" : "", dimmed(n.ref) ? "dim" : "",
        this.changed.has(n.ref) ? "changed" : ""].join(" ");
      const el = s("g", { class: cls, transform: `translate(${b.x},${b.y})`, "data-id": n.ref, tabindex: 0 },
        s("rect", { class: "box", width: NODE_W, height: NODE_H, rx: 8, stroke: color }),
        s("rect", { class: "bar", width: 6, height: NODE_H, rx: 3, fill: color }),
        s("text", { class: "title", x: 14, y: 20 }, trim(n.name, 26)),
        s("text", { class: "ref", x: 14, y: 36 }, trim(n.ref, 34)),
        n.has_proposal
          ? s("text", { class: "val", x: 14, y: 54 }, trim(valText, 12), s("tspan", { class: "val proposal" }, ` → ${fmtValue(n.proposed_value)}`))
          : s("text", { class: "val", x: 14, y: 54 }, trim(valText, 30)),
        flags ? s("text", { class: "flag", x: NODE_W - 8, y: 18, "text-anchor": "end", fill: color }, flags) : null,
        n.formula_rule ? s("text", { class: "ref", x: NODE_W - 8, y: 54, "text-anchor": "end" }, "ƒ") : null,
        s("title", {}, `${n.name}（${n.ref}）\n値: ${valText}${n.formula ? `\n算出式: ${n.formula}` : ""}${n.proposed_error ? `\n再計算エラー: ${n.proposed_error}` : ""}`));
      this.bindNode(el, n.ref, "param");
      root.append(el);
    }
    this.applyTransform();
  }

  bindNode(el, id, kind) {
    el.addEventListener("mousedown", (ev) => {
      if (ev.button !== 0) return;
      ev.stopPropagation();
      const start = this.toGraph(ev);
      const b = this.boxesCache.get(id);
      const origin = kind === "rule" ? { x: b.ox, y: b.oy } : { x: b.x, y: b.y };
      let moved = false;
      const move = (e) => {
        const p = this.toGraph(e);
        const dx = p.x - start.x;
        const dy = p.y - start.y;
        if (!moved && Math.hypot(dx, dy) < 4 / this.view.k) return;
        moved = true;
        this.saved[id] = [Math.round(origin.x + dx), Math.round(origin.y + dy)];
        this.draw();
      };
      const up = (e) => {
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
        if (moved) this.persist();
        else this.onSelect(id, kind, e);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    });
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") this.onSelect(id, kind, e); });
  }

  bindPanZoom() {
    const svg = this.svg;
    svg.addEventListener("mousedown", (ev) => {
      if (ev.button !== 0) return;
      const start = { x: ev.clientX, y: ev.clientY, vx: this.view.x, vy: this.view.y };
      let moved = false;
      svg.classList.add("panning");
      const move = (e) => {
        if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 3) moved = true;
        this.view.x = start.vx + (e.clientX - start.x);
        this.view.y = start.vy + (e.clientY - start.y);
        this.applyTransform();
      };
      const up = () => {
        svg.classList.remove("panning");
        window.removeEventListener("mousemove", move);
        window.removeEventListener("mouseup", up);
        if (!moved) this.onSelect(null, null);
      };
      window.addEventListener("mousemove", move);
      window.addEventListener("mouseup", up);
    });
    svg.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = Math.exp(-e.deltaY * 0.0015);
      this.zoomAt(e.clientX, e.clientY, factor);
    }, { passive: false });
  }

  zoomAt(clientX, clientY, factor) {
    const rect = this.svg.getBoundingClientRect();
    const k = Math.min(3, Math.max(0.15, this.view.k * factor));
    const px = clientX - rect.left;
    const py = clientY - rect.top;
    this.view.x = px - ((px - this.view.x) * k) / this.view.k;
    this.view.y = py - ((py - this.view.y) * k) / this.view.k;
    this.view.k = k;
    this.applyTransform();
  }

  zoom(factor) {
    const rect = this.svg.getBoundingClientRect();
    this.zoomAt(rect.left + rect.width / 2, rect.top + rect.height / 2, factor);
  }

  fit() {
    if (!this.graph) return;
    const rect = this.svg.getBoundingClientRect();
    const boxes = [...this.boxes().values()];
    if (!boxes.length || !rect.width) return;
    const minX = Math.min(...boxes.map((b) => b.x)) - 40;
    const minY = 0;
    const maxX = Math.max(...boxes.map((b) => b.x + b.w)) + 40;
    const maxY = Math.max(...boxes.map((b) => b.y + b.h)) + 30;
    const k = Math.min(1.2, Math.max(0.15, Math.min(rect.width / (maxX - minX), rect.height / (maxY - minY))));
    this.view = { k, x: (rect.width - (maxX - minX) * k) / 2 - minX * k, y: 10 - minY * k };
    this.applyTransform();
  }

  center(id) {
    const b = this.boxesCache && this.boxesCache.get(id);
    if (!b) return;
    const rect = this.svg.getBoundingClientRect();
    this.view.x = rect.width / 2 - (b.x + b.w / 2) * this.view.k;
    this.view.y = rect.height / 2 - (b.y + b.h / 2) * this.view.k;
    this.applyTransform();
  }

  applyTransform() {
    this.root.setAttribute("transform", `translate(${this.view.x},${this.view.y}) scale(${this.view.k})`);
  }

  toGraph(e) {
    const rect = this.svg.getBoundingClientRect();
    return { x: (e.clientX - rect.left - this.view.x) / this.view.k, y: (e.clientY - rect.top - this.view.y) / this.view.k };
  }

  /** 影響範囲（上流・下流）を計算する。dir: up / down / both */
  trace(id, dir) {
    const edges = this.graph.edges;
    const out = new Set([id]);
    const walk = (forward) => {
      const queue = [id];
      const seen = new Set([id]);
      while (queue.length) {
        const cur = queue.shift();
        for (const e of edges) {
          const [a, b] = forward ? [e.from, e.to] : [e.to, e.from];
          if (a === cur && !seen.has(b)) { seen.add(b); out.add(b); queue.push(b); }
        }
      }
    };
    if (dir === "down" || dir === "both") walk(true);
    if (dir === "up" || dir === "both") walk(false);
    return out;
  }
}

function marker(id, color) {
  return s("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" },
    s("path", { d: "M0,0 L10,5 L0,10 z", fill: color }));
}

function edgePath(a, b) {
  const sameCol = Math.abs(a.x - b.x) < 60 && Math.abs(a.x + a.w - (b.x + b.w)) < 60;
  if (sameCol) {
    const sx = a.x + a.w;
    const sy = a.y + a.h / 2;
    const tx = b.x + b.w;
    const ty = b.y + b.h / 2;
    const c = 50 + Math.min(80, Math.abs(ty - sy) / 4);
    return `M${sx},${sy} C${sx + c},${sy} ${tx + c},${ty} ${tx},${ty}`;
  }
  const forward = b.x > a.x;
  const sx = forward ? a.x + a.w : a.x;
  const tx = forward ? b.x : b.x + b.w;
  const sy = a.y + a.h / 2;
  const ty = b.y + b.h / 2;
  const mx = (sx + tx) / 2;
  return `M${sx},${sy} C${mx},${sy} ${mx},${ty} ${tx},${ty}`;
}
