import pytest

from reqviz.engine.graph import CycleError, find_cycle, reachable, topo_sort


def test_topo_sort_order_and_stability():
    edges = [("a", "c"), ("b", "c"), ("c", "d")]
    assert topo_sort(["d", "c", "b", "a"], edges) == ["b", "a", "c", "d"]


def test_topo_sort_includes_edge_only_nodes():
    assert topo_sort([], [("x", "y")]) == ["x", "y"]


def test_topo_sort_cycle():
    with pytest.raises(CycleError) as ei:
        topo_sort(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
    cyc = ei.value.cycle
    assert cyc[0] == cyc[-1] and set(cyc) == {"a", "b", "c"}


def test_find_cycle_none_and_self_loop():
    assert find_cycle(["a", "b"], [("a", "b")]) is None
    assert find_cycle(["a"], [("a", "a")]) == ["a", "a"]


def test_find_cycle_in_later_component():
    cyc = find_cycle(["a", "b", "x", "y"], [("a", "b"), ("x", "y"), ("y", "x")])
    assert set(cyc) == {"x", "y"}


def test_reachable_down_and_up():
    edges = [("a", "b"), ("b", "c"), ("x", "c")]
    assert reachable(["a"], edges) == {"b", "c"}
    assert reachable(["c"], edges, reverse=True) == {"a", "b", "x"}
    assert reachable(["c"], edges) == set()
