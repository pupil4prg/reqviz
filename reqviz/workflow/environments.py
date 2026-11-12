"""環境（本番・検証・開発など）ごとの管理。

1 つの環境は 1 つのシステムとして持ち、同じシステムの環境違いを「環境グループ」で束ねる。
このため、値・承認・版・履歴は環境ごとに独立する（本番の版 v3 と開発の版 v7 が並立できる）。
環境をまたぐ操作として、次の 3 つを提供する。

- 比較: パラメータ・構成項目・ルールを環境の間で並べ、値や定義の違いを示す
- 構成の同期: ある環境の定義（レイヤー・パラメータの定義・ルールなど）を他の環境に反映する。値は反映先のものを保つ
- 値のコピー: 選んだパラメータの値・採用案を、ある環境から別の環境へコピーする
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from ..context import Ctx
from ..db import transaction
from ..engine.model import Param, check_value_type
from ..errors import Invalid
from ..repo import history
from ..repo import model as repo
from . import editing, items, snapshot, templates

_KEEP_FROM_TARGET = ("rationale", "ref_doc", "owner", "tags", "source")  # 環境ごとに持つ付加情報


# ---------------------------------------------------------------- 環境グループ

def _env_dict(r: Any, current_id: int) -> dict[str, Any]:
    d = dict(r)
    d["is_current"] = d["id"] == current_id
    return d


def list_environments(ctx: Ctx, system_id: int) -> list[dict[str, Any]]:
    """同じ環境グループのシステム（自分を含む）。環境を分けていなければ自分だけを返す。"""
    s = repo.get_system(ctx, system_id)
    where, arg = ("s.env_group_id = ?", s["env_group_id"]) if s["env_group_id"] else ("s.id = ?", system_id)
    rows = ctx.conn.execute(
        f"""
        SELECT s.id, s.name, s.env_group_id, s.env_key, s.env_name, s.updated_at,
               (SELECT COUNT(*) FROM params p WHERE p.system_id = s.id) AS param_count,
               (SELECT COUNT(*) FROM params p WHERE p.system_id = s.id AND p.status = 'approved') AS approved_count,
               (SELECT COUNT(*) FROM items i WHERE i.system_id = s.id) AS item_count,
               b.version_no AS current_baseline_version
        FROM systems s LEFT JOIN baselines b ON b.id = s.current_baseline_id
        WHERE {where} ORDER BY s.id
        """,
        (arg,),
    )
    return [_env_dict(r, system_id) for r in rows]


def _label(data: dict[str, Any], key_field: str, name_field: str) -> tuple[str, str]:
    key = editing.validate_identifier(data.get(key_field), "環境キー")
    name = data.get(name_field)
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 32:
        raise Invalid("環境名は 1〜32 文字で指定してください")
    return key, name.strip()


def _check_key_free(ctx: Ctx, group_id: int, key: str, exclude_id: int | None = None) -> None:
    row = ctx.conn.execute("SELECT id FROM systems WHERE env_group_id = ? AND env_key = ?", (group_id, key)).fetchone()
    if row and row["id"] != exclude_id:
        raise Invalid(f"環境キーが重複しています: {key}")


def create_environment(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """このシステムを元に、新しい環境を作る（構成と値をコピーし、承認はやり直し）。

    data: key, name（新しい環境）。まだ環境を分けていないシステムでは、
    current_key, current_name（元のシステム自身の環境名）も必要。
    """
    src = repo.get_system(ctx, system_id)
    key, name = _label(data, "key", "name")
    with transaction(ctx.conn):
        group_id = src["env_group_id"]
        if not group_id:
            cur_key, cur_name = _label(data, "current_key", "current_name")
            if cur_key == key:
                raise Invalid(f"環境キーが重複しています: {key}")
            group_id = system_id
            ctx.conn.execute(
                "UPDATE systems SET env_group_id = ?, env_key = ?, env_name = ? WHERE id = ?", (group_id, cur_key, cur_name, system_id)
            )
            history.audit(ctx, system_id, "environment.init", "system", cur_key, {"name": cur_name})
        _check_key_free(ctx, group_id, key)
        new = editing.create_system(ctx, {"name": src["name"], "description": src["description"]})
        snapshot.replace_with_payload(
            ctx, new["id"], snapshot.export_payload(ctx, system_id), keep_status=False, op="create",
            comment=f"環境 {src['env_name'] or data.get('current_name')} から作成",
        )
        templates._copy_flags(ctx, system_id, new["id"])
        ctx.conn.execute(
            "UPDATE systems SET env_group_id = ?, env_key = ?, env_name = ?, template_id = ?, template_version = ? WHERE id = ?",
            (group_id, key, name, src["template_id"], src["template_version"], new["id"]),
        )
        ctx.conn.execute("UPDATE params SET flag_upstream_changed = 0 WHERE system_id = ?", (new["id"],))
        history.audit(ctx, new["id"], "environment.create", "system", key, {"name": name, "from_system": system_id})
    return repo.get_system(ctx, new["id"])


def update_environment(ctx: Ctx, system_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """環境のキー・名前を変更する。"""
    s = repo.get_system(ctx, system_id)
    if not s["env_group_id"]:
        raise Invalid("このシステムは環境を分けていません")
    key, name = _label({"key": data.get("key", s["env_key"]), "name": data.get("name", s["env_name"])}, "key", "name")
    with transaction(ctx.conn):
        _check_key_free(ctx, s["env_group_id"], key, exclude_id=system_id)
        ctx.conn.execute("UPDATE systems SET env_key = ?, env_name = ? WHERE id = ?", (key, name, system_id))
        history.audit(ctx, system_id, "environment.update", "system", key, {"name": name})
    return repo.get_system(ctx, system_id)


def detach(ctx: Ctx, system_id: int) -> dict[str, Any]:
    """環境グループから外し、独立したシステムにする（データは変わらない）。"""
    s = repo.get_system(ctx, system_id)
    if not s["env_group_id"]:
        raise Invalid("このシステムは環境を分けていません")
    with transaction(ctx.conn):
        ctx.conn.execute("UPDATE systems SET env_group_id = NULL, env_key = '', env_name = '' WHERE id = ?", (system_id,))
        history.audit(ctx, system_id, "environment.detach", "system", s["env_key"])
        rest = ctx.conn.execute("SELECT id FROM systems WHERE env_group_id = ?", (s["env_group_id"],)).fetchall()
        if len(rest) == 1:  # 1 つだけ残ったグループは解消する
            ctx.conn.execute("UPDATE systems SET env_group_id = NULL, env_key = '', env_name = '' WHERE id = ?", (rest[0]["id"],))
    return repo.get_system(ctx, system_id)


def _siblings(ctx: Ctx, system_id: int) -> dict[int, dict[str, Any]]:
    envs = list_environments(ctx, system_id)
    if len(envs) < 2:
        raise Invalid("このシステムには、他の環境がありません")
    return {e["id"]: e for e in envs}


def _check_target(envs: dict[int, dict[str, Any]], source_id: int, target_id: Any) -> int:
    if not isinstance(target_id, int) or isinstance(target_id, bool) or target_id not in envs or target_id == source_id:
        raise Invalid(f"反映先には、同じ環境グループの別の環境を指定してください: {target_id!r}")
    return target_id


# ---------------------------------------------------------------- 比較

def _digest(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= 80 else text[:80] + "…"


def compare(ctx: Ctx, system_id: int) -> dict[str, Any]:
    """環境の間で、パラメータ・構成項目・ルールを並べて比較する。

    各行の ``values`` は環境（システム ID の文字列）ごとの値で、その環境にない場合は入らない。
    ``differs`` は値（採用案・本文）の違い、``missing_in`` はその項目がない環境、
    ``definition_differs`` は定義（名前・型・単位・選択肢など）が食い違っているフィールド。
    """
    envs = list_environments(ctx, system_id)
    ids = [e["id"] for e in envs]
    order = [system_id] + [i for i in ids if i != system_id]  # 表示の基準は現在の環境

    def rows(load: Any, value_of: Any, def_fields: tuple[str, ...], extra: Any) -> list[dict[str, Any]]:
        per_env = {i: {x["ref"]: x for x in load(i)} for i in ids}
        refs: list[str] = []
        for i in order:
            refs += [r for r in per_env[i] if r not in refs]
        out = []
        for ref in refs:
            present = [i for i in ids if ref in per_env[i]]
            base = per_env[present[0] if system_id not in present else system_id][ref]
            values = {str(i): {"value": value_of(per_env[i][ref]), "status": per_env[i][ref]["status"], "id": per_env[i][ref]["id"]} for i in present}
            distinct = {json.dumps(v["value"], ensure_ascii=False, sort_keys=True) for v in values.values()}
            out.append({
                "ref": ref, "name": base["name"], "layer_key": base["layer_key"], **extra(base),
                "values": values,
                "differs": len(distinct) > 1,
                "missing_in": [i for i in ids if i not in present],
                "definition_differs": [f for f in def_fields if len({json.dumps(per_env[i][ref][f], ensure_ascii=False, sort_keys=True) for i in present}) > 1],
            })
        return out

    params = rows(lambda i: repo.list_params(ctx, i), lambda p: p["value"], ("name", "type", "enum_options", "unit", "aliases", "component"),
                  lambda p: {"unit": p["unit"], "type": p["type"]})
    item_rows = rows(
        lambda i: repo.list_items(ctx, i),
        lambda x: x["selected"] if x["kind"] == "choice" else _digest(x["content"]) if x["content"] is not None else None,
        ("name", "kind", "options", "content_type", "component"), lambda x: {"kind": x["kind"]},
    )

    rules_env = {i: {r["key"]: snapshot._rule_out(r) for r in repo.list_rules(ctx, i)} for i in ids}
    rule_rows = []
    for key in sorted({k for i in ids for k in rules_env[i]}):
        present = [i for i in ids if key in rules_env[i]]
        bodies = {str(i): rules_env[i][key] for i in present}
        if len(present) < len(ids) or len({json.dumps(b, ensure_ascii=False, sort_keys=True) for b in bodies.values()}) > 1:
            rule_rows.append({"key": key, "rules": bodies, "missing_in": [i for i in ids if i not in present]})

    def count(rs: list[dict[str, Any]], f: Any) -> int:
        return sum(1 for r in rs if f(r))

    structural = lambda r: bool(r["missing_in"] or r["definition_differs"])  # noqa: E731
    return {
        "environments": envs,
        "params": params,
        "items": item_rows,
        "rules": rule_rows,
        "summary": {
            "value_differences": count(params, lambda r: r["differs"]) + count(item_rows, lambda r: r["differs"]),
            "structure_differences": count(params, structural) + count(item_rows, structural) + len(rule_rows),
        },
    }


# ---------------------------------------------------------------- 構成の同期

def _value_ok(definition: dict[str, Any], value: Any) -> bool:
    probe = Param(ref="(value)", type=definition["type"], enum_options=definition.get("enum_options") or [])
    return check_value_type(probe, value) is None


def merge_structure(src: dict[str, Any], tgt: dict[str, Any], delete_extra: bool = False) -> dict[str, Any]:
    """反映元（src）の定義と、反映先（tgt）の値を合わせたスナップショットを作る（純粋関数）。

    - 定義（レイヤー、コンポーネント、パラメータの名前・型・単位・別名・所属、選択肢の候補、ルール、関連）は src に合わせる
    - 値・採用案・本文と、根拠・出典・担当・タグは tgt のものを保つ。型が変わって合わなくなった値は未設定にする
    - 値が保たれた項目は承認状態も保ち、それ以外（新しく増えた項目など）は下書きにする
    - tgt にだけある項目は、``delete_extra`` が真なら消し、偽なら残す
    """
    def ref_of(x: dict[str, Any]) -> str:
        return f"{x['layer']}.{x['key']}"

    def union(section: str, keyf: Any) -> list[dict[str, Any]]:
        out = [dict(x) for x in src.get(section, [])]
        if not delete_extra:
            have = {keyf(x) for x in out}
            out += [dict(x) for x in tgt.get(section, []) if keyf(x) not in have]
        return out

    out: dict[str, Any] = {"format": snapshot.FORMAT, "format_version": snapshot.FORMAT_VERSION, "system": dict(tgt.get("system", {}))}
    for section in ("layers", "components"):
        out[section] = [{**x, "sort_order": i} for i, x in enumerate(union(section, lambda x: x["key"]))]
    comp_keys = {c["key"] for c in out["components"]}
    out["component_deps"] = [d for d in union("component_deps", lambda x: (x["from"], x["to"])) if d["from"] in comp_keys and d["to"] in comp_keys]

    tgt_params = {ref_of(p): p for p in tgt.get("params", [])}
    params = []
    for p in src.get("params", []):
        m, t = dict(p), tgt_params.get(ref_of(p))
        if t is None:
            m.update(status="draft", ai_skipped=False)  # 新しい項目。値は反映元のものを出発点にする
        else:
            for f in _KEEP_FROM_TARGET:
                m[f] = t.get(f)
            m["value"] = t.get("value") if _value_ok(p, t.get("value")) else None
            kept = m["value"] == t.get("value")
            m["status"] = t.get("status", "draft") if kept else "draft"
            m["ai_skipped"] = bool(t.get("ai_skipped")) if kept else False
        params.append(m)
    if not delete_extra:
        have = {ref_of(p) for p in params}
        params += [dict(p) for ref, p in tgt_params.items() if ref not in have]
    out["params"] = params

    tgt_items = {ref_of(i): i for i in tgt.get("items", [])}
    merged_items = []
    for i in src.get("items", []):
        m, t = dict(i), tgt_items.get(ref_of(i))
        if t is None or t.get("kind") != i.get("kind"):
            m["status"] = "draft"
        else:
            for f in ("rationale", "ref_doc", "owner", "tags"):
                m[f] = t.get(f)
            if i["kind"] == "choice":
                keys = [o["key"] for o in i.get("options", [])]
                m["selected"] = t.get("selected") if t.get("selected") in keys else None
                kept = m["selected"] == t.get("selected") and i.get("options") == t.get("options")
            else:  # 不定形データは本文そのものが環境ごとの値
                m["content_type"], m["content"] = t.get("content_type"), t.get("content")
                kept = True
            m["status"] = t.get("status", "draft") if kept else "draft"
        merged_items.append(m)
    if not delete_extra:
        have = {ref_of(i) for i in merged_items}
        merged_items += [dict(i) for ref, i in tgt_items.items() if ref not in have]
    out["items"] = merged_items

    out["rules"] = union("rules", lambda x: x["key"])
    refs = {ref_of(p) for p in params}
    out["relations"] = [r for r in union("relations", lambda x: (x["from"], x["to"])) if r["from"] in refs and r["to"] in refs]
    return out


def sync_structure(
    ctx: Ctx, source_id: int, target_ids: Iterable[Any], delete_extra: bool = False, preview: bool = False, comment: str = ""
) -> dict[str, Any]:
    """反映元の環境の構成（定義とルール）を、他の環境に反映する。値は反映先のものを保つ。

    ``preview`` が真なら、反映した場合の差分だけを返して何も変更しない。
    """
    envs = _siblings(ctx, source_id)
    targets = [_check_target(envs, source_id, t) for t in dict.fromkeys(target_ids)]
    if not targets:
        raise Invalid("反映先の環境を指定してください")
    src_payload = snapshot.export_payload(ctx, source_id)
    src_name = envs[source_id]["env_name"]
    results = []
    with transaction(ctx.conn):
        for tid in targets:
            before = snapshot.export_payload(ctx, tid)
            merged = merge_structure(src_payload, before, delete_extra)
            diff = snapshot.diff_payloads(before, merged)
            changed = sum(len(v) for v in diff.values())
            if not preview and changed:
                flags = {
                    p["ref"]: (p["flag_stale"], p["flag_upstream_changed"], p["proposed_value"], p["proposed_error"], p["has_proposal"])
                    for p in repo.list_params(ctx, tid)
                }
                old_formulas = {r["target"]: r["expression"] for r in before["rules"] if r["kind"] == "formula" and r["enabled"]}
                snapshot.replace_with_payload(
                    ctx, tid, merged, keep_status=True, op="update", comment=comment or f"環境 {src_name} から構成を同期",
                )
                _restore_flags(ctx, tid, flags, old_formulas)
                history.audit(ctx, tid, "environment.sync", "system", envs[tid]["env_key"],
                              {"from": envs[source_id]["env_key"], "changes": changed, "delete_extra": delete_extra}, comment)
            results.append({"system_id": tid, "env_key": envs[tid]["env_key"], "env_name": envs[tid]["env_name"],
                            "changes": changed, "diff": diff})
    return {"preview": preview, "source": {"system_id": source_id, "env_key": envs[source_id]["env_key"]}, "targets": results}


def _restore_flags(ctx: Ctx, system_id: int, flags: dict[str, Any], old_formulas: dict[str, Any]) -> None:
    """置き換えで消えたフラグと再計算案を戻し、算出式が変わった・増えたパラメータを要再計算にする。"""
    from ..db import dumps

    by_ref = repo.params_by_ref(ctx, system_id)
    for ref, (stale, upstream, proposed, error, has_proposal) in flags.items():
        if ref in by_ref:
            ctx.conn.execute(
                "UPDATE params SET flag_stale = ?, flag_upstream_changed = ?, proposed_value = ?, proposed_error = ? WHERE id = ?",
                (int(stale), int(upstream), dumps(proposed) if has_proposal else None, error, by_ref[ref]["id"]),
            )
    changed_targets = [
        r["target"] for r in repo.list_rules(ctx, system_id)
        if r["kind"] == "formula" and r["enabled"] and r["target"] in by_ref and old_formulas.get(r["target"]) != r["expression"]
    ]
    editing._after_rule_change(ctx, system_id, changed_targets)


# ---------------------------------------------------------------- 値のコピー

def copy_values(ctx: Ctx, source_id: int, target_id: Any, refs: Iterable[str], comment: str = "") -> dict[str, Any]:
    """選んだパラメータの値・構成項目の採用案や本文を、別の環境へコピーする。

    反映先では通常の変更として扱う（履歴に残り、値が変わった項目は下書きに戻る）。
    算出式で決まるパラメータは対象外（反映先で再計算する）。
    """
    envs = _siblings(ctx, source_id)
    target_id = _check_target(envs, source_id, target_id)
    refs = list(dict.fromkeys(refs))
    if not refs:
        raise Invalid("コピーする項目の参照名（refs）を指定してください")
    src_params, src_items = repo.params_by_ref(ctx, source_id), repo.items_by_ref(ctx, source_id)
    tgt_params, tgt_items = repo.params_by_ref(ctx, target_id), repo.items_by_ref(ctx, target_id)
    formula_targets = {r["target"] for r in repo.list_rules(ctx, target_id) if r["kind"] == "formula" and r["enabled"]}
    comment = comment or f"環境 {envs[source_id]['env_name']} から値をコピー"
    copied, skipped = [], []
    with transaction(ctx.conn):
        for ref in refs:
            if ref in src_params and ref in tgt_params:
                if ref in formula_targets:
                    skipped.append({"ref": ref, "reason": "算出式で決まる値のため（反映先で再計算してください）"})
                elif src_params[ref]["value"] == tgt_params[ref]["value"]:
                    skipped.append({"ref": ref, "reason": "値が同じ"})
                else:
                    try:
                        editing.update_param(ctx, tgt_params[ref]["id"], {"value": src_params[ref]["value"]}, comment=comment)
                        copied.append(ref)
                    except Invalid as e:
                        skipped.append({"ref": ref, "reason": e.message})
            elif ref in src_items and ref in tgt_items and src_items[ref]["kind"] == tgt_items[ref]["kind"]:
                s, t = src_items[ref], tgt_items[ref]
                data = {"selected": s["selected"]} if s["kind"] == "choice" else {"content_type": s["content_type"], "content": s["content"]}
                if all(t[k] == v for k, v in data.items()):
                    skipped.append({"ref": ref, "reason": "内容が同じ"})
                else:
                    try:
                        items.update_item(ctx, t["id"], data, comment=comment)
                        copied.append(ref)
                    except Invalid as e:
                        skipped.append({"ref": ref, "reason": e.message})
            else:
                skipped.append({"ref": ref, "reason": "どちらかの環境に存在しない（先に構成を同期してください）"})
        history.audit(ctx, target_id, "environment.copy_values", "system", envs[target_id]["env_key"],
                      {"from": envs[source_id]["env_key"], "copied": copied}, comment)
    return {"copied": copied, "skipped": skipped}
