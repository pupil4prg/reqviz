"""コンポーネントと、コンポーネント間の依存関係。

レイヤーが設計工程（業務要件 → … → インフラ設計）の軸なのに対して、コンポーネントは
構成要素（AP サーバー、ミドルウェア、OS など）の軸である。パラメータと構成項目は、
レイヤーとは別にコンポーネントを 1 つ持てる。

依存関係 ``from → to`` は「from の設計が to に波及する」ことを表す（例: ミドルウェア → OS）。
宣言した依存は、式・制約・関連から導いた実際のつながりと突き合わせて検証できる。
"""

from __future__ import annotations

from typing import Any, Iterable

from ..context import Ctx
from ..db import transaction
from ..engine import calc
from ..engine.graph import find_cycle, reachable
from ..errors import Invalid, NotFound
from ..repo import history
from ..repo import model as repo
from . import editing, items

_SNAPSHOT = ("key", "name", "kind", "description")


def _component_dict(r: Any) -> dict[str, Any]:
    return dict(r)


def list_components(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(
        """
        SELECT c.*,
               (SELECT COUNT(*) FROM params p WHERE p.component_id = c.id) AS param_count,
               (SELECT COUNT(*) FROM items i WHERE i.component_id = c.id) AS item_count
        FROM components c WHERE c.system_id = ? ORDER BY c.sort_order, c.id
        """,
        (system_id,),
    )
    return [_component_dict(r) for r in rows]


def get_component(ctx: Ctx, component_id: int) -> dict[str, Any]:
    r = ctx.conn.execute("SELECT * FROM components WHERE id = ?", (component_id,)).fetchone()
    if r is None:
        raise NotFound(f"コンポーネントが見つかりません: {component_id}")
    return next(c for c in list_components(ctx, r["system_id"]) if c["id"] == component_id)


def _fields(data: dict[str, Any], current: dict[str, Any] | None) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if "key" in data or current is None:
        fields["key"] = editing.validate_identifier(data.get("key"), "コンポーネントキー")
    if "name" in data or current is None:
        fields["name"] = editing.require_text(data, "name", "コンポーネント名")
    for f, limit in (("kind", 32), ("description", 2000)):
        if f in data or current is None:
            v = data.get(f) or ""
            if not isinstance(v, str) or len(v) > limit:
                raise Invalid(f"{f} は {limit} 文字以内の文字列で指定してください")
            fields[f] = v.strip()
    if "sort_order" in data and data["sort_order"] is not None:
        fields["sort_order"] = int(data["sort_order"])
    return fields


def _check_key(ctx: Ctx, system_id: int, key: str, exclude_id: int | None) -> None:
    row = ctx.conn.execute("SELECT id FROM components WHERE system_id = ? AND key = ?", (system_id, key)).fetchone()
    if row and row["id"] != exclude_id:
        raise Invalid(f"コンポーネントキーが重複しています: {key}")


def create_component(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    repo.get_system(ctx, system_id)
    fields = _fields(data, None)
    with transaction(ctx.conn):
        _check_key(ctx, system_id, fields["key"], None)
        if "sort_order" not in fields:
            fields["sort_order"] = ctx.conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM components WHERE system_id = ?", (system_id,)
            ).fetchone()[0]
        fields["system_id"] = system_id
        cols = ", ".join(fields)
        cur = ctx.conn.execute(
            f"INSERT INTO components ({cols}) VALUES ({', '.join('?' * len(fields))})", list(fields.values())
        )
        c = get_component(ctx, cur.lastrowid)
        history.add_revision(ctx, system_id, "component", c["key"], "create", after={k: c[k] for k in _SNAPSHOT})
        editing._touch_system(ctx, system_id)
    return c


def update_component(ctx: Ctx, component_id: int, data: dict[str, Any]) -> dict[str, Any]:
    before = get_component(ctx, component_id)
    fields = _fields(data, before)
    with transaction(ctx.conn):
        if fields.get("key", before["key"]) != before["key"]:
            _check_key(ctx, before["system_id"], fields["key"], component_id)
        editing._update_row(ctx, "components", component_id, fields, data.get("row_version"))
        after = get_component(ctx, component_id)
        history.add_revision(
            ctx, before["system_id"], "component", after["key"], "update",
            before={k: before[k] for k in _SNAPSHOT}, after={k: after[k] for k in _SNAPSHOT},
        )
        editing._touch_system(ctx, before["system_id"])
    return after


def delete_component(ctx: Ctx, component_id: int) -> None:
    """コンポーネントを削除する。所属していたパラメータ・構成項目は「未分類」に戻り、関係する依存は消える。"""
    c = get_component(ctx, component_id)
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM components WHERE id = ?", (component_id,))
        history.add_revision(
            ctx, c["system_id"], "component", c["key"], "delete",
            before={**{k: c[k] for k in _SNAPSHOT}, "param_count": c["param_count"], "item_count": c["item_count"]},
        )
        editing._touch_system(ctx, c["system_id"])


def assign(ctx: Ctx, system_id: int, refs: Iterable[str], component: Any, comment: str = "") -> dict[str, Any]:
    """複数のパラメータ・構成項目を、まとめてコンポーネントに割り当てる（component が空なら未分類に戻す）。"""
    key = component or None
    if key is not None and not any(c["key"] == key for c in list_components(ctx, system_id)):
        raise Invalid(f"コンポーネントが存在しません: {key}")
    targets = {**{r: ("item", i) for r, i in repo.items_by_ref(ctx, system_id).items()},
               **{r: ("param", p) for r, p in repo.params_by_ref(ctx, system_id).items()}}
    refs = list(dict.fromkeys(refs))
    unknown = [r for r in refs if r not in targets]
    if unknown:
        raise Invalid(f"存在しない参照名です: {', '.join(unknown[:10])}")
    updated = []
    with transaction(ctx.conn):
        for ref in refs:
            kind, row = targets[ref]
            if row["component"] == key:
                continue
            if kind == "param":
                editing.update_param(ctx, row["id"], {"component": key}, comment=comment)
            else:
                items.update_item(ctx, row["id"], {"component": key}, comment=comment)
            updated.append(ref)
    return {"updated": updated, "unchanged": [r for r in refs if r not in updated]}


# ---------------------------------------------------------------- 依存関係

_DEP_SQL = """
SELECT d.*, f.key AS from_key, f.name AS from_name, t.key AS to_key, t.name AS to_name
FROM component_deps d JOIN components f ON f.id = d.from_component JOIN components t ON t.id = d.to_component
"""


def _dep_dict(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["notify"] = bool(d["notify"])
    return d


def list_deps(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    rows = ctx.conn.execute(_DEP_SQL + " WHERE d.system_id = ? ORDER BY f.sort_order, f.id, t.sort_order, t.id", (system_id,))
    return [_dep_dict(r) for r in rows]


def get_dep(ctx: Ctx, dep_id: int) -> dict[str, Any]:
    r = ctx.conn.execute(_DEP_SQL + " WHERE d.id = ?", (dep_id,)).fetchone()
    if r is None:
        raise NotFound(f"依存関係が見つかりません: {dep_id}")
    return _dep_dict(r)


def _dep_note(data: dict[str, Any]) -> str:
    note = data.get("note") or ""
    if not isinstance(note, str) or len(note) > 2000:
        raise Invalid("note は 2000 文字以内の文字列で指定してください")
    return note


def create_dep(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """依存関係を定義する。data: from（波及元のキー）, to（波及先のキー）, note, notify。"""
    by_key = {c["key"]: c for c in list_components(ctx, system_id)}
    f, t = data.get("from"), data.get("to")
    if f not in by_key or t not in by_key:
        raise Invalid("依存の両端（from / to）には、既存のコンポーネントのキーを指定してください")
    if f == t:
        raise Invalid("同じコンポーネント同士の依存は定義できません")
    note = _dep_note(data)
    with transaction(ctx.conn):
        if ctx.conn.execute(
            "SELECT 1 FROM component_deps WHERE from_component = ? AND to_component = ?", (by_key[f]["id"], by_key[t]["id"])
        ).fetchone():
            raise Invalid(f"依存 {f} → {t} は既に定義されています")
        cur = ctx.conn.execute(
            "INSERT INTO component_deps (system_id, from_component, to_component, note, notify) VALUES (?, ?, ?, ?, ?)",
            (system_id, by_key[f]["id"], by_key[t]["id"], note, 1 if data.get("notify") else 0),
        )
        history.add_revision(
            ctx, system_id, "component_dep", f"{f}->{t}", "create", after={"note": note, "notify": bool(data.get("notify"))}
        )
        editing._touch_system(ctx, system_id)
    return get_dep(ctx, cur.lastrowid)


def update_dep(ctx: Ctx, dep_id: int, data: dict[str, Any]) -> dict[str, Any]:
    before = get_dep(ctx, dep_id)
    fields: dict[str, Any] = {}
    if "note" in data:
        fields["note"] = _dep_note(data)
    if "notify" in data:
        fields["notify"] = 1 if data["notify"] else 0
    with transaction(ctx.conn):
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            ctx.conn.execute(f"UPDATE component_deps SET {sets} WHERE id = ?", [*fields.values(), dep_id])
        after = get_dep(ctx, dep_id)
        history.add_revision(
            ctx, before["system_id"], "component_dep", f"{before['from_key']}->{before['to_key']}", "update",
            before={"note": before["note"], "notify": before["notify"]}, after={"note": after["note"], "notify": after["notify"]},
        )
        editing._touch_system(ctx, before["system_id"])
    return after


def delete_dep(ctx: Ctx, dep_id: int) -> None:
    d = get_dep(ctx, dep_id)
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM component_deps WHERE id = ?", (dep_id,))
        history.add_revision(
            ctx, d["system_id"], "component_dep", f"{d['from_key']}->{d['to_key']}", "delete",
            before={"note": d["note"], "notify": d["notify"]},
        )
        editing._touch_system(ctx, d["system_id"])


# ---------------------------------------------------------------- 分析

def _levels(keys: list[str], edges: list[tuple[str, str]]) -> dict[str, int]:
    """図の列（波及元ほど左）。循環があっても止まるよう、たどる深さを制限する。"""
    preds: dict[str, set[str]] = {k: set() for k in keys}
    for a, b in edges:
        if a in preds and b in preds and a != b:
            preds[b].add(a)
    level = {k: 0 for k in keys}
    for _ in range(len(keys)):
        changed = False
        for k in keys:
            want = max((level[p] + 1 for p in preds[k]), default=0)
            if want > level[k] and want < len(keys):
                level[k] = want
                changed = True
        if not changed:
            break
    return level


def analysis(ctx: Ctx, system_id: int) -> dict[str, Any]:
    """宣言した依存と、式・制約・関連から導いた実際のつながりを突き合わせる。

    - ``deps``: 宣言した依存。``links`` はそれを裏付けるパラメータ間のつながり（なければ裏付けなし）
    - ``undeclared``: 実際にはつながっているのに、依存として宣言されていないコンポーネントの組
    - ``unassigned``: コンポーネントが未設定のパラメータ・構成項目
    - ``cycles``: 宣言した依存の循環
    - ``components[].upstream / downstream``: 宣言した依存を推移的にたどった波及元・波及先
    """
    repo.get_system(ctx, system_id)
    comps = list_components(ctx, system_id)
    deps = list_deps(ctx, system_id)
    params, all_items = repo.list_params(ctx, system_id), repo.list_items(ctx, system_id)
    comp_of = {x["ref"]: x["component"] for x in params + all_items}

    # パラメータ間のつながりを、コンポーネントの組ごとに集める
    directed: dict[tuple[str, str], list[dict[str, Any]]] = {}
    undirected: dict[frozenset[str], list[dict[str, Any]]] = {}
    for r in repo.list_rules(ctx, system_id):
        if not r["enabled"] or r["kind"] == "nl":
            continue
        refs = sorted(x for x in calc.rule_refs(repo.to_engine_rule(r)) if comp_of.get(x))
        if r["kind"] == "formula":
            tc = comp_of.get(r["target"])
            for src in refs:
                if tc and src != r["target"] and comp_of[src] != tc:
                    directed.setdefault((comp_of[src], tc), []).append(
                        {"kind": "derives", "from_ref": src, "to_ref": r["target"], "rule_key": r["key"]})
        else:
            for i, a in enumerate(refs):
                for b in refs[i + 1:]:
                    if comp_of[a] != comp_of[b]:
                        undirected.setdefault(frozenset((comp_of[a], comp_of[b])), []).append(
                            {"kind": "constrains", "from_ref": a, "to_ref": b, "rule_key": r["key"]})
    for rel in repo.list_relations(ctx, system_id):
        fc, tc = comp_of.get(rel["from_ref"]), comp_of.get(rel["to_ref"])
        if fc and tc and fc != tc:
            directed.setdefault((fc, tc), []).append(
                {"kind": "influences", "from_ref": rel["from_ref"], "to_ref": rel["to_ref"], "rule_key": None})

    declared = {(d["from_key"], d["to_key"]) for d in deps}
    out_deps = []
    for d in deps:
        pair = (d["from_key"], d["to_key"])
        links = directed.get(pair, []) + undirected.get(frozenset(pair), [])
        out_deps.append({**d, "links": links, "supported": bool(links)})

    undeclared = [
        {"from": f, "to": t, "links": links} for (f, t), links in sorted(directed.items()) if (f, t) not in declared
    ]
    for pair, links in sorted(undirected.items(), key=lambda kv: sorted(kv[0])):
        a, b = sorted(pair)
        if (a, b) not in declared and (b, a) not in declared:
            # 制約には向きがないので、どちら向きでも宣言されていなければ未宣言として挙げる
            undeclared.append({"from": a, "to": b, "links": links, "undirected": True})

    keys = [c["key"] for c in comps]
    dep_edges = sorted(declared)
    cycle = find_cycle(keys, dep_edges)
    level = _levels(keys, dep_edges + [(u["from"], u["to"]) for u in undeclared if not u.get("undirected")])
    out_comps = []
    for c in comps:
        out_comps.append({
            **c, "level": level[c["key"]],
            "downstream": sorted(reachable([c["key"]], dep_edges) - {c["key"]}),
            "upstream": sorted(reachable([c["key"]], dep_edges, reverse=True) - {c["key"]}),
        })
    return {
        "components": out_comps,
        "deps": out_deps,
        "undeclared": undeclared,
        "unassigned": {
            "params": [p["ref"] for p in params if not p["component"]],
            "items": [i["ref"] for i in all_items if not i["component"]],
        },
        "cycles": [cycle] if cycle else [],
    }
