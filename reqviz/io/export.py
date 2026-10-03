"""CSV / HTML 形式の書き出し（JSON は workflow.snapshot を使う）。"""

from __future__ import annotations

import csv
import io
from html import escape
from typing import Any

from ..context import Ctx
from ..db import now
from ..engine.layout import COL_WIDTH, ROW_HEIGHT
from ..repo import model as repo
from ..workflow import analysis, baseline
from ..workflow.param_state import STATUS_LABELS

CSV_COLUMNS = [
    ("layer_name", "レイヤー"), ("ref", "参照名"), ("name", "名前"), ("type", "型"),
    ("value", "値"), ("unit", "単位"), ("status", "状態"), ("source", "値の出所"),
    ("formula", "算出式"), ("rationale", "根拠"), ("ref_doc", "出典"), ("owner", "担当"),
    ("flag_stale", "要再計算"), ("flag_upstream_changed", "承認後に上流変更あり"), ("violation", "制約違反"),
]

SOURCE_LABELS = {"manual": "手入力", "formula": "算出式", "ai": "AI提案"}


def fmt_value(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def to_csv(ctx: Ctx, system_id: int) -> bytes:
    g = analysis.graph(ctx, system_id)
    layer_names = {l["key"]: l["name"] for l in g["layers"]}
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow([label for _, label in CSV_COLUMNS])
    for n in g["nodes"]:
        row = {
            **n,
            "layer_name": layer_names.get(n["layer_key"], n["layer_key"]),
            "value": fmt_value(n["value"]),
            "status": STATUS_LABELS.get(n["status"], n["status"]),
            "source": SOURCE_LABELS.get(n["source"], n["source"]),
            "formula": n["formula"] or "",
            "flag_stale": "○" if n["flag_stale"] else "",
            "flag_upstream_changed": "○" if n["flag_upstream_changed"] else "",
            "violation": n["violation"] or "",
        }
        w.writerow([row.get(k, "") for k, _ in CSV_COLUMNS])
    return buf.getvalue().encode("utf-8-sig")  # Excel で文字化けしないよう BOM 付き


# ---------------------------------------------------------------- SVG

NODE_COLORS = {
    "error": "#d93025", "stale": "#e8710a", "upstream": "#8e44ad",
    "approved": "#188038", "in_review": "#1a73e8", "ai_evaluated": "#12b5cb", "draft": "#80868b",
}


def node_color(n: dict[str, Any]) -> str:
    if n.get("violation") == "error":
        return NODE_COLORS["error"]
    if n.get("flag_stale"):
        return NODE_COLORS["stale"]
    if n.get("flag_upstream_changed"):
        return NODE_COLORS["upstream"]
    return NODE_COLORS.get(n["status"], NODE_COLORS["draft"])


def _trim(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def render_svg(g: dict[str, Any]) -> str:
    """グラフデータ（analysis.graph の結果）から静的 SVG を生成する。"""
    W, H = g["size"]["width"], g["size"]["height"]
    pos: dict[str, tuple[float, float, float, float]] = {}
    for n in g["nodes"]:
        pos[n["ref"]] = (n["x"], n["y"], COL_WIDTH, ROW_HEIGHT)
    for rn in g["rule_nodes"]:
        pos[rn["id"]] = (rn["x"] + 30, rn["y"] + 12, COL_WIDTH - 60, ROW_HEIGHT - 24)
    layer_names = {l["key"]: l["name"] for l in g["layers"]}

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        'font-family="sans-serif" font-size="12">',
        '<defs><marker id="arr" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="#888"/></marker></defs>',
    ]
    for col in g["columns"]:
        out.append(
            f'<text x="{col["x"] + COL_WIDTH / 2}" y="30" text-anchor="middle" font-size="14" font-weight="bold" fill="#333">'
            f'{escape(layer_names.get(col["layer_key"], col["layer_key"]))}</text>'
        )
    dash = {"derives": "", "constrains": ' stroke-dasharray="6 4"', "influences": ' stroke-dasharray="2 4"'}
    for e in g["edges"]:
        if e["from"] not in pos or e["to"] not in pos:
            continue
        x1, y1, w1, h1 = pos[e["from"]]
        x2, y2, w2, h2 = pos[e["to"]]
        sx, sy = (x1 + w1, y1 + h1 / 2) if x2 > x1 else (x1, y1 + h1 / 2)
        tx, ty = (x2, y2 + h2 / 2) if x2 > x1 else (x2 + w2, y2 + h2 / 2)
        if abs(x2 - x1) < 1:  # 同じレイヤー内
            sx, tx = x1 + w1, x2 + w2
            c = 60
            d = f"M{sx},{sy} C{sx + c},{sy} {tx + c},{ty} {tx},{ty}"
        else:
            mx = (sx + tx) / 2
            d = f"M{sx},{sy} C{mx},{sy} {mx},{ty} {tx},{ty}"
        out.append(f'<path d="{d}" fill="none" stroke="#999" stroke-width="1.4"{dash[e["kind"]]} marker-end="url(#arr)"/>')
    for n in g["nodes"]:
        x, y, w, h = pos[n["ref"]]
        color = node_color(n)
        value = fmt_value(n["value"]) or "（未設定）"
        unit = f" {n['unit']}" if n["unit"] else ""
        out.append(
            f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#fff" stroke="{color}" stroke-width="2"/>'
            f'<rect x="{x}" y="{y}" width="6" height="{h}" rx="3" fill="{color}"/>'
            f'<text x="{x + 14}" y="{y + 20}" font-weight="bold" fill="#202124">{escape(_trim(n["name"], 16))}</text>'
            f'<text x="{x + 14}" y="{y + 38}" fill="#5f6368" font-size="11">{escape(n["ref"])}</text>'
            f'<text x="{x + 14}" y="{y + 55}" fill="#202124">{escape(_trim(value + unit, 24))}</text></g>'
        )
    for rn in g["rule_nodes"]:
        x, y, w, h = pos[rn["id"]]
        color = NODE_COLORS["error"] if rn["violation"] and rn["severity"] == "error" else (
            "#f9ab00" if rn["violation"] else "#9aa0a6")
        out.append(
            f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h / 2}" fill="#f8f9fa" stroke="{color}" stroke-width="1.5" stroke-dasharray="4 3"/>'
            f'<text x="{x + w / 2}" y="{y + h / 2 + 4}" text-anchor="middle" fill="#3c4043" font-size="11">'
            f'{escape(rn["key"])}: {escape(_trim(rn["description"] or rn["expression"] or "", 14))}</text></g>'
        )
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- HTML 設計書

_CSS = """
body{font-family:"Segoe UI","Hiragino Sans","Meiryo",sans-serif;margin:24px;color:#202124;background:#fff}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px;border-bottom:2px solid #dadce0;padding-bottom:4px}
.meta{color:#5f6368;font-size:13px}.graph{overflow:auto;border:1px solid #dadce0;border-radius:8px;padding:8px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{border:1px solid #dadce0;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f1f3f4}code{font-family:Consolas,monospace;font-size:12px}.error{color:#d93025;font-weight:bold}.warning{color:#b06000;font-weight:bold}
"""


def to_html(ctx: Ctx, system_id: int) -> bytes:
    g = analysis.graph(ctx, system_id)
    s = g["system"]
    layer_names = {l["key"]: l["name"] for l in g["layers"]}
    rules = repo.list_rules(ctx, system_id)
    bl = None
    if s["current_baseline_id"]:
        bl = baseline.get_baseline(ctx, s["current_baseline_id"], with_payload=False)

    h: list[str] = [
        "<!doctype html><html lang='ja'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{escape(s['name'])} 設計パラメータ</title><style>{_CSS}</style></head><body>",
        f"<h1>{escape(s['name'])} 設計パラメータ</h1>",
        f"<div class='meta'>出力日時: {escape(now())}"
        + (f" ／ 現行ベースライン: v{bl['version_no']}（{escape(bl['decided_at'] or '')} 承認）" if bl else " ／ 承認済みの版なし")
        + "</div>",
    ]
    if s["description"]:
        h.append(f"<p>{escape(s['description'])}</p>")
    h.append("<h2>関連図</h2><div class='graph'>" + render_svg(g) + "</div>")

    h.append("<h2>パラメータ一覧</h2>")
    for layer in g["layers"]:
        ns = [n for n in g["nodes"] if n["layer_key"] == layer["key"]]
        if not ns:
            continue
        h.append(f"<h3>{escape(layer['name'])}</h3><table><tr><th>参照名</th><th>名前</th><th>値</th><th>単位</th>"
                 "<th>状態</th><th>算出式</th><th>根拠</th><th>出典</th></tr>")
        for n in ns:
            h.append(
                f"<tr><td><code>{escape(n['ref'])}</code></td><td>{escape(n['name'])}</td>"
                f"<td>{escape(fmt_value(n['value']))}</td><td>{escape(n['unit'])}</td>"
                f"<td>{escape(STATUS_LABELS.get(n['status'], n['status']))}</td>"
                f"<td><code>{escape(n['formula'] or '')}</code></td><td>{escape(n['rationale'])}</td>"
                f"<td>{escape(n['ref_doc'])}</td></tr>"
            )
        h.append("</table>")

    h.append("<h2>ルール一覧</h2><table><tr><th>キー</th><th>種別</th><th>対象</th><th>内容</th><th>重要度</th><th>説明</th></tr>")
    kind_labels = {"formula": "算出式", "constraint": "制約", "nl": "自然言語"}
    for r in rules:
        body = r["expression"] or r["nl_text"] or ""
        h.append(
            f"<tr><td>{escape(r['key'])}{'' if r['enabled'] else '（無効）'}</td><td>{kind_labels[r['kind']]}</td>"
            f"<td><code>{escape(r['target'] or '')}</code></td><td><code>{escape(body)}</code></td>"
            f"<td>{escape(r['severity'])}</td><td>{escape(r['description'])}</td></tr>"
        )
    h.append("</table>")

    h.append("<h2>制約チェック結果</h2>")
    if g["violations"]:
        h.append("<table><tr><th>ルール</th><th>重要度</th><th>内容</th></tr>")
        for v in g["violations"]:
            h.append(f"<tr><td>{escape(v['rule_key'])}</td><td class='{v['severity']}'>{v['severity']}</td><td>{escape(v['message'])}</td></tr>")
        h.append("</table>")
    else:
        h.append("<p>違反はありません。</p>")
    h.append("</body></html>")
    return "".join(h).encode("utf-8")
