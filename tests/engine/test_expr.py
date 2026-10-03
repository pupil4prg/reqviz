import pytest

from reqviz.engine import expr


@pytest.mark.parametrize(
    "text, env, expected",
    [
        ("1 + 2 * 3", {}, 7),
        ("a.x / 4", {"a.x": 10}, 2.5),
        ("a.x // 4", {"a.x": 10}, 2),
        ("a.x % 4", {"a.x": 10}, 2),
        ("2 ** 10", {}, 1024),
        ("-a.x", {"a.x": 3}, -3),
        ("ceil(a.x * 0.2)", {"a.x": 11}, 3),
        ("floor(2.7)", {}, 2),
        ("round(2.567, 1)", {}, 2.6),
        ("min(a.x, 5)", {"a.x": 7}, 5),
        ("max(1, 2, 3)", {}, 3),
        ("abs(-4)", {}, 4),
        ("sum(1, 2, 3)", {}, 6),
        ("0.5 if biz.t == 'online' else 0.2", {"biz.t": "online"}, 0.5),
        ("0.5 if biz.t == 'online' else 0.2", {"biz.t": "batch"}, 0.2),
        ("1 < a.x <= 3", {"a.x": 3}, True),
        ("1 < a.x <= 3", {"a.x": 4}, False),
        ("biz.t in ('a', 'b')", {"biz.t": "b"}, True),
        ("biz.t not in ['a', 'b']", {"biz.t": "c"}, True),
        ("a.f and not a.g", {"a.f": True, "a.g": False}, True),
        ("a.f or a.g", {"a.f": False, "a.g": 0}, 0),
        ("a.f and a.g", {"a.f": 0, "a.g": 1}, 0),
        ("'x' + a.s", {"a.s": "y"}, "xy"),
    ],
)
def test_evaluate(text, env, expected):
    assert expr.evaluate(text, env) == expected


def test_references():
    assert expr.references("ceil(app.users * db.ratio) + app.users") == {"app.users", "db.ratio"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "x + 1",  # 裸の名前
        "a.b.c",  # 3 段の属性
        "__import__('os')",
        "open('f')",
        "a.x.__class__",
        "[i for i in (1, 2)]",
        "lambda: 1",
        "a.x[0]",
        "max(*a.x)",
        "round(1.5, ndigits=1)",
        "1 +",
        "f'{a.x}'",
        "(y := 1)",
        "b'bytes'",
        "1 & 2",
        "a.x is None",
        "1" * 2000,
        "-" * 40 + "1",  # ネスト上限
    ],
)
def test_parse_rejects(text):
    with pytest.raises(expr.ExprError):
        expr.parse(text)


@pytest.mark.parametrize(
    "text, env, message",
    [
        ("a.x / 0", {"a.x": 1}, "0 で除算"),
        ("a.x + 1", {}, "未定義"),
        ("a.x + 1", {"a.x": None}, "未設定"),
        ("a.x + 1", {"a.x": "s"}, "評価エラー"),
        ("2 ** 1000", {}, "指数"),
        ("ceil('a')", {}, "評価エラー"),
    ],
)
def test_evaluate_errors(text, env, message):
    with pytest.raises(expr.EvalError, match=message):
        expr.evaluate(text, env)


def test_check():
    assert expr.check("a.x + b.y", {"a.x", "b.y"}) == []
    assert expr.check("a.x + b.y", {"a.x"}) == ["未定義のパラメータです: b.y"]
    assert expr.check("a.x +", None)[0].startswith("構文エラー")
    assert expr.check("a.x + b.y") == []
