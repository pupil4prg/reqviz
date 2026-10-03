import pytest

from reqviz.engine.calc import formula_edges, recalc, validate_rules
from reqviz.engine.graph import CycleError
from reqviz.engine.model import Param, Rule, check_value_type


def web3():
    params = [
        Param("biz.type", "enum", "online", ["online", "batch"]),
        Param("biz.tps", "number", 100),
        Param("biz.resp", "number", 0.5),
        Param("app.conc", "number", None),
        Param("app.servers", "number", 2),
        Param("app.threads", "number", None),
        Param("db.ratio", "number", None),
        Param("db.pool", "number", None),
    ]
    rules = [
        Rule("r1", "formula", "ceil(biz.tps * biz.resp * 1.2)", "app.conc"),
        Rule("r2", "formula", "ceil(app.conc / app.servers)", "app.threads"),
        Rule("r3", "formula", "0.5 if biz.type == 'online' else 0.2", "db.ratio"),
        Rule("r4", "formula", "ceil(app.threads * db.ratio)", "db.pool"),
    ]
    return params, rules


def test_recalc_all_chains():
    params, rules = web3()
    res = recalc(params, rules)
    assert res["app.conc"].new_value == 60
    assert res["app.threads"].new_value == 30
    assert res["db.ratio"].new_value == 0.5
    assert res["db.pool"].new_value == 15
    assert all(r.changed for r in res.values())


def test_recalc_from_start_only_downstream():
    params, rules = web3()
    res = recalc(params, rules, starts=["app.servers"])
    assert set(res) == {"app.threads", "db.pool"}
    # app.conc は未計算(None)なので app.threads は失敗し、下流も失敗する
    assert "未設定" in res["app.threads"].error
    assert res["db.pool"].error


def test_recalc_include_starts_false():
    params, rules = web3()
    res = recalc(params, rules, starts=["app.conc"], include_starts=False)
    assert "app.conc" not in res and "app.threads" in res


def test_recalc_unchanged_and_int_normalization():
    params, _ = web3()
    p = {x.ref: x for x in params}
    p["app.conc"].value = 60
    rules = [Rule("r", "formula", "app.conc / 2", "app.threads")]
    res = recalc(params, rules)
    assert res["app.threads"].new_value == 30 and isinstance(res["app.threads"].new_value, int)
    p["app.threads"].value = 30
    assert not recalc(params, rules)["app.threads"].changed


def test_recalc_type_mismatch():
    params = [Param("a.x", "number", 1), Param("a.y", "number", None)]
    res = recalc(params, [Rule("r", "formula", "'s'", "a.y")])
    assert "数値が必要" in res["a.y"].error


def test_recalc_disabled_rule_ignored():
    params, rules = web3()
    rules[0].enabled = False
    assert "app.conc" not in recalc(params, rules)


def test_recalc_cycle_raises():
    params = [Param("a.x", "number", 1), Param("a.y", "number", 1)]
    rules = [Rule("r1", "formula", "a.y + 1", "a.x"), Rule("r2", "formula", "a.x + 1", "a.y")]
    with pytest.raises(CycleError):
        recalc(params, rules)


def test_validate_rules_ok():
    params, rules = web3()
    assert validate_rules(params, rules) == {}


def test_validate_rules_errors():
    params, _ = web3()
    rules = [
        Rule("bad_syntax", "formula", "biz.tps +", "app.conc"),
        Rule("unknown_ref", "constraint", "zz.q > 1"),
        Rule("no_target", "formula", "1"),
        Rule("missing_target", "formula", "1", "zz.q"),
        Rule("dup1", "formula", "1", "db.pool"),
        Rule("dup2", "formula", "2", "db.pool"),
        Rule("self", "formula", "app.threads + 1", "app.threads"),
        Rule("empty_nl", "nl", nl_text=" "),
        Rule("nl_bad_target", "nl", nl_text="x", target="zz.q"),
        Rule("empty_constraint", "constraint", ""),
        Rule("weird", "other"),
    ]
    errs = validate_rules(params, rules)
    assert set(errs) == {
        "bad_syntax", "unknown_ref", "no_target", "missing_target", "dup2",
        "self", "empty_nl", "nl_bad_target", "empty_constraint", "weird",
    }
    assert any("循環" in m for m in errs["self"])


def test_validate_rules_cycle_marks_members():
    params = [Param("a.x"), Param("a.y"), Param("a.z")]
    rules = [
        Rule("r1", "formula", "a.y", "a.x"),
        Rule("r2", "formula", "a.x", "a.y"),
        Rule("r3", "formula", "a.x", "a.z"),
    ]
    assert set(validate_rules(params, rules)) == {"r1", "r2"}


def test_formula_edges_skips_unparsable():
    rules = [Rule("r", "formula", "a.x +", "a.y"), Rule("s", "formula", "a.x + a.w", "a.z")]
    assert formula_edges(rules) == [("a.w", "a.z"), ("a.x", "a.z")]


@pytest.mark.parametrize(
    "ptype, opts, value, ok",
    [
        ("number", [], 1.5, True),
        ("number", [], True, False),
        ("number", [], "1", False),
        ("string", [], "s", True),
        ("string", [], 1, False),
        ("enum", ["a", "b"], "a", True),
        ("enum", ["a", "b"], "c", False),
        ("bool", [], False, True),
        ("bool", [], 0, False),
        ("number", [], None, True),
    ],
)
def test_check_value_type(ptype, opts, value, ok):
    assert (check_value_type(Param("a.x", ptype, enum_options=opts), value) is None) == ok
