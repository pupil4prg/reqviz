"""値変更時のフラグ伝播（要再計算・承認後上流変更あり）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping

from .graph import reachable


@dataclass
class FlagUpdate:
    stale: set[str] = field(default_factory=set)
    upstream_changed: set[str] = field(default_factory=set)


def propagate_change(
    changed: Iterable[str],
    formula_edges: Iterable[tuple[str, str]],
    statuses: Mapping[str, str],
) -> FlagUpdate:
    """``changed`` の値が変わったときに、フラグを立てるべき下流パラメータを返す。

    - 算出式で推移的に依存する下流すべてに「要再計算」
    - そのうち承認済みのものに「承認後に上流変更あり」
    """
    changed = list(changed)
    downstream = reachable(changed, formula_edges) - set(changed)
    upstream_changed = {r for r in downstream if statuses.get(r) == "approved"}
    return FlagUpdate(stale=downstream, upstream_changed=upstream_changed)
