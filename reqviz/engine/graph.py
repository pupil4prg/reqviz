"""依存グラフ操作（トポロジカルソート、循環検出、影響範囲探索）。

ノードは任意のハッシュ可能値、エッジは ``(from, to)`` のタプル。
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Hashable, Iterable, TypeVar

N = TypeVar("N", bound=Hashable)


class CycleError(Exception):
    """依存関係に循環がある。``cycle`` に循環パス（始点 = 終点）を持つ。"""

    def __init__(self, cycle: list):
        self.cycle = cycle
        super().__init__("依存関係が循環しています: " + " → ".join(map(str, cycle)))


def adjacency(edges: Iterable[tuple[N, N]], reverse: bool = False) -> dict[N, list[N]]:
    adj: dict[N, list[N]] = defaultdict(list)
    for a, b in edges:
        if reverse:
            adj[b].append(a)
        else:
            adj[a].append(b)
    return adj


def find_cycle(nodes: Iterable[N], edges: Iterable[tuple[N, N]]) -> list[N] | None:
    """循環があればそのパス（始点 = 終点）を、なければ None を返す。"""
    adj = adjacency(edges)
    all_nodes = list(dict.fromkeys(list(nodes) + list(adj)))
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[N, int] = {n: WHITE for n in all_nodes}
    for n in adj.values():
        for m in n:
            color.setdefault(m, WHITE)

    for start in list(color):
        if color[start] != WHITE:
            continue
        # 反復 DFS（再帰上限を避ける）
        stack: list[tuple[N, int]] = [(start, 0)]
        path: list[N] = []
        while stack:
            node, idx = stack.pop()
            if idx == 0:
                color[node] = GRAY
                path.append(node)
            children = adj.get(node, [])
            if idx < len(children):
                stack.append((node, idx + 1))
                child = children[idx]
                if color.get(child, WHITE) == GRAY:
                    i = path.index(child)
                    return path[i:] + [child]
                if color.get(child, WHITE) == WHITE:
                    stack.append((child, 0))
            else:
                color[node] = BLACK
                path.pop()
    return None


def topo_sort(nodes: Iterable[N], edges: Iterable[tuple[N, N]]) -> list[N]:
    """Kahn 法によるトポロジカルソート。循環時は CycleError。

    同順位のノードは入力順を保つ（結果を安定させるため）。
    """
    edges = list(edges)
    order_nodes = list(dict.fromkeys(list(nodes) + [x for e in edges for x in e]))
    pos = {n: i for i, n in enumerate(order_nodes)}
    indeg: dict[N, int] = {n: 0 for n in order_nodes}
    adj = adjacency(edges)
    for _, b in edges:
        indeg[b] += 1
    queue = deque(sorted((n for n in order_nodes if indeg[n] == 0), key=pos.__getitem__))
    result: list[N] = []
    while queue:
        n = queue.popleft()
        result.append(n)
        for m in sorted(adj.get(n, []), key=pos.__getitem__):
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    if len(result) != len(order_nodes):
        cycle = find_cycle(order_nodes, edges)
        raise CycleError(cycle or [])
    return result


def reachable(starts: Iterable[N], edges: Iterable[tuple[N, N]], reverse: bool = False) -> set[N]:
    """``starts`` から到達可能なノード集合（``starts`` 自身は含まない。ただし循環で戻る場合は含む）。

    ``reverse=True`` なら上流方向に辿る。
    """
    adj = adjacency(edges, reverse=reverse)
    seen: set[N] = set()
    queue = deque(starts)
    while queue:
        n = queue.popleft()
        for m in adj.get(n, []):
            if m not in seen:
                seen.add(m)
                queue.append(m)
    return seen
