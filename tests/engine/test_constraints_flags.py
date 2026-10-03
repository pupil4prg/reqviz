from reqviz.engine.constraints import evaluate_constraints
from reqviz.engine.flags import propagate_change
from reqviz.engine.model import Rule


def test_constraints():
    values = {"db.pool": 15, "app.servers": 2, "db.max": 25, "os.proc": None}
    rules = [
        Rule("ok", "constraint", "db.pool * app.servers <= 40"),
        Rule("ng", "constraint", "db.pool * app.servers <= db.max", description="DB接続上限"),
        Rule("warn", "constraint", "db.pool < 10", severity="warning"),
        Rule("unset", "constraint", "os.proc > 1", severity="warning"),
        Rule("disabled", "constraint", "False", enabled=False),
        Rule("formula", "formula", "1", "db.pool"),
    ]
    vs = {v.rule_key: v for v in evaluate_constraints(values, rules)}
    assert set(vs) == {"ng", "warn", "unset"}
    assert vs["ng"].kind == "violated" and vs["ng"].severity == "error"
    assert "DB接続上限" in vs["ng"].message and "db.max=25" in vs["ng"].message
    assert vs["ng"].refs == ["app.servers", "db.max", "db.pool"]
    assert vs["unset"].kind == "eval_error"
    assert vs["warn"].to_dict()["severity"] == "warning"


def test_propagate_change():
    edges = [("a", "b"), ("b", "c"), ("x", "c"), ("c", "d")]
    statuses = {"b": "draft", "c": "approved", "d": "approved"}
    up = propagate_change(["a"], edges, statuses)
    assert up.stale == {"b", "c", "d"}
    assert up.upstream_changed == {"c", "d"}
    assert propagate_change(["d"], edges, statuses).stale == set()
