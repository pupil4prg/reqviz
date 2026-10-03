from reqviz.engine.layout import COL_GAP, COL_WIDTH, layout


def test_columns_follow_layer_order():
    lay = layout(["a", "b"], [("x", "b"), ("y", "a")], [("y", "x")])
    assert lay.positions["y"][0] < lay.positions["x"][0]
    assert lay.positions["x"][0] - lay.positions["y"][0] == COL_WIDTH + COL_GAP
    assert [k for k, _ in lay.columns] == ["a", "b"]


def test_barycenter_reduces_crossing():
    # 左列 a1, a2 / 右列 b1, b2。a1→b2, a2→b1 なら右列は b2, b1 の順になるべき
    lay = layout(["L", "R"], [("a1", "L"), ("a2", "L"), ("b1", "R"), ("b2", "R")], [("a1", "b2"), ("a2", "b1")])
    p = lay.positions
    assert (p["a1"][1] < p["a2"][1]) == (p["b2"][1] < p["b1"][1])


def test_unknown_layer_ignored_and_empty():
    lay = layout(["a"], [("x", "zz")], [])
    assert lay.positions == {}
    assert layout([], [], []).width > 0
