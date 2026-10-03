"""レイヤー型グラフの簡易レイアウト（Sugiyama 法の簡略版）。

- x 座標: レイヤーの順序（左 → 右）
- y 座標: 各レイヤー内で、隣接ノードの重心順に並べ替えて交差を減らす
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Hashable, Iterable

COL_WIDTH = 230
COL_GAP = 90
ROW_HEIGHT = 64
ROW_GAP = 22
MARGIN = 40
HEADER = 44
SWEEPS = 4


@dataclass
class Layout:
    positions: dict[Hashable, tuple[float, float]]
    width: float
    height: float
    columns: list[tuple[str, float]]  # (layer_key, x)


def layout(
    layers: list[str],
    nodes: Iterable[tuple[Hashable, str]],
    edges: Iterable[tuple[Hashable, Hashable]],
) -> Layout:
    """nodes: (ノードID, レイヤーキー)、edges: (from, to)。座標はノード左上。"""
    col_of_layer = {k: i for i, k in enumerate(layers)}
    columns: list[list[Hashable]] = [[] for _ in layers]
    col_of: dict[Hashable, int] = {}
    for nid, layer in nodes:
        c = col_of_layer.get(layer)
        if c is None:
            continue
        columns[c].append(nid)
        col_of[nid] = c

    neighbors: dict[Hashable, list[Hashable]] = defaultdict(list)
    for a, b in edges:
        if a in col_of and b in col_of and a != b:
            neighbors[a].append(b)
            neighbors[b].append(a)

    def positions_index() -> dict[Hashable, float]:
        idx: dict[Hashable, float] = {}
        for col in columns:
            off = (len(col) - 1) / 2
            for i, n in enumerate(col):
                idx[n] = i - off  # 列の中心を 0 に揃えて列間で比較できるようにする
        return idx

    def sweep(order: range, use_left: bool) -> None:
        for c in order:
            idx = positions_index()
            col = columns[c]
            keyed = []
            for i, n in enumerate(col):
                ns = [idx[m] for m in neighbors[n] if (col_of[m] < c if use_left else col_of[m] > c)]
                bary = sum(ns) / len(ns) if ns else idx[n]
                keyed.append((bary, i, n))
            keyed.sort(key=lambda t: (t[0], t[1]))
            columns[c] = [n for _, _, n in keyed]

    for _ in range(SWEEPS):
        sweep(range(1, len(columns)), use_left=True)
        sweep(range(len(columns) - 2, -1, -1), use_left=False)

    tallest = max((len(c) for c in columns), default=0)
    total_h = tallest * ROW_HEIGHT + max(tallest - 1, 0) * ROW_GAP
    pos: dict[Hashable, tuple[float, float]] = {}
    col_x = []
    for c, col in enumerate(columns):
        x = MARGIN + c * (COL_WIDTH + COL_GAP)
        col_x.append((layers[c], x))
        h = len(col) * ROW_HEIGHT + max(len(col) - 1, 0) * ROW_GAP
        y0 = MARGIN + HEADER + (total_h - h) / 2
        for i, n in enumerate(col):
            pos[n] = (x, y0 + i * (ROW_HEIGHT + ROW_GAP))
    width = MARGIN * 2 + max(len(columns), 1) * COL_WIDTH + max(len(columns) - 1, 0) * COL_GAP
    height = MARGIN * 2 + HEADER + total_h
    return Layout(pos, width, height, col_x)
