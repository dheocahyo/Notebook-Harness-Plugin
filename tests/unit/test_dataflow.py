"""Defs and uses per cell, downstream stale marking, and the names cells above define (L120)."""

from __future__ import annotations

import pytest

from nh_gateway.dataflow import EVERYTHING, analyze, defined_names, defs_uses, downstream


def defs(source: str) -> set[str]:
    return defs_uses(source)[0]


def uses(source: str) -> set[str]:
    return defs_uses(source)[1]


# defs -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("x = 1", {"x"}),
        ("a, (b, *c) = f()", {"a", "b", "c"}),
        ("x: int = 1", {"x"}),
        ("for i, row in rows:\n    pass", {"i", "row"}),
        ("with open(p) as fh, lock as (a, b):\n    pass", {"fh", "a", "b"}),
        ("if (n := len(xs)) > 3:\n    pass", {"n"}),
        ("[y for x in xs if (y := x)]", {"y"}),
        ("import os.path, numpy as np", {"os", "np"}),
        ("from pandas import DataFrame as DF, Series", {"DF", "Series"}),
        ("def f():\n    pass\nclass C:\n    pass", {"f", "C"}),
        ("async def g():\n    await h()", {"g"}),
        ("del old", {"old"}),
        ("del d['k'], obj.attr", {"d", "obj"}),
        (
            "match cmd:\n    case [first, *rest]:\n        pass\n    case {'k': v, **others}:\n        pass",
            {"first", "rest", "v", "others"},
        ),
    ],
)
def test_bindings_are_defs(source: str, expected: set[str]) -> None:
    assert defs(source) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("df['p'] = 0", {"df"}),
        ("df.loc[mask, 'p'] = 0", {"df"}),
        ("obj.attr = 1", {"obj"}),
        ("obj.a.b[0] = 1", {"obj"}),
        ("counts['a'] += 1", {"counts"}),
        ("total += 1", {"total"}),
        ("df.dropna(inplace=True)", {"df"}),
        ("df['p'].fillna(0, inplace=True)", {"df"}),
        ("model.fit(X_train, y_train)", {"model", "X_train", "y_train"}),
        ("items.append(row)", {"items", "row"}),
        ("await client.refresh()", {"client"}),
        ("shuffle(items)", {"items"}),
        ("np.random.shuffle(arr)", {"np", "arr"}),
        ("update(*parts, into=target)", {"parts", "target"}),
        ("df.groupby('a').apply(f)", {"f"}),  # arguments of any call statement may change
    ],
)
def test_mutations_are_defs(source: str, expected: set[str]) -> None:
    assert defs(source) == expected


@pytest.mark.parametrize(
    "source",
    [
        "print(df)",
        "display(df)",
        "len(df)",
        "df.head()",
        "df.describe()",
        "df.to_csv(path)",
        "df.plot()",
        "logger.info(msg)",
        "get_frame().fit()",
    ],
)
def test_read_only_statements_define_nothing(source: str) -> None:
    assert defs(source) == set()


def test_global_assignment_inside_a_function_is_a_def() -> None:
    assert defs("def reset():\n    global counter\n    counter = 0") == {"reset", "counter"}


def test_except_name_is_not_a_def() -> None:
    assert defs("try:\n    f()\nexcept KeyError as err:\n    print(err)") == set()


def test_type_params_are_local() -> None:
    source = "def first[T](items: list[T]) -> T:\n    return items[0]\nclass Box[K]:\n    k: K"
    try:
        compile(source, "<cell>", "exec")
    except SyntaxError:
        pytest.skip("type parameters need Python 3.12")
    assert defs_uses(source) == ({"first", "Box"}, {"list"}, True)


# uses -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("y = x + 1", {"x"}),
        ("x = x + 1", {"x"}),
        ("x = 1\ny = x", set()),
        ("df_clean = df.dropna()\ndf_clean.head()", {"df"}),
        ("if c:\n    q = 1\nprint(q)", {"c", "q", "print"}),  # q may not exist after the if
        ("with ctx() as v:\n    w = v", {"ctx"}),
        ("for i in xs:\n    total = i\nprint(total)", {"xs", "total", "print"}),
        (
            "try:\n    import foo\nexcept ImportError:\n    foo = None\nfoo.run()",
            {"ImportError", "foo"},
        ),
        ("[v * k for v in values]", {"values", "k"}),
        ("{k: v for k, v in pairs}", {"pairs"}),
        ("f = lambda r: r.a + offset", {"offset"}),
        ("sorted(xs, key=lambda x: x[1])", {"sorted", "xs"}),
        ("df['p'] = 0", {"df"}),
        ("total += 1", {"total"}),
        ("del old", {"old"}),
        ("class A(Base):\n    k = K\n    j = k", {"Base", "K"}),
    ],
)
def test_uses_are_reads_before_binding(source: str, expected: set[str]) -> None:
    assert uses(source) == expected


def test_function_bodies_read_globals_at_call_time() -> None:
    source = "def f(a):\n    return helper(a) + g\ndef helper(b):\n    return b\nf(1)"
    assert uses(source) == {"g"}


def test_nested_function_and_nonlocal_are_local() -> None:
    source = "def outer():\n    n = 0\n    def inner():\n        nonlocal n\n        n += 1\n    return inner"
    assert uses(source) == set()


def test_decorators_and_defaults_are_read_where_defined() -> None:
    assert uses("@register(REG)\ndef f(x=DEFAULT, *, y: Hint = 1):\n    pass") == {
        "register",
        "REG",
        "DEFAULT",
        "Hint",
    }


def test_comprehension_first_iterable_is_read_outside() -> None:
    assert uses("pairs = [(a, b) for a in left for b in right if a < b]") == {"left", "right"}


# magics and unparsable cells --------------------------------------------------------------------
def test_unparsable_cell_uses_everything() -> None:
    assert defs_uses("x = (") == (set(), {EVERYTHING}, False)


def test_line_magics() -> None:
    assert defs_uses("%time y = f(x)") == ({"y"}, {"f", "x"}, True)
    assert defs_uses("files = !ls\nprint(files)") == ({"files"}, {"print"}, True)
    assert defs_uses("%matplotlib inline") == (set(), set(), True)


@pytest.mark.parametrize(
    ("source", "expected_defs"),
    [
        ("%%time\nz = compute(a)", {"z"}),
        ("%%capture out\nprint(1)", {"out"}),
        ("%%capture --no-stderr cap\nprint(1)", {"cap"}),
        ("%%bash --out res --err problems\nls", {"res", "problems"}),
        ("%%sql\nSELECT 1", set()),
        ("%%time\nx = (", set()),
    ],
)
def test_cell_magics_use_everything(source: str, expected_defs: set[str]) -> None:
    assert defs_uses(source) == (expected_defs, {EVERYTHING}, False)


# downstream -------------------------------------------------------------------------------------
CELLS = [
    ("load", "df = pd.read_csv(PATH)"),
    ("clean", "df_clean = df.dropna()"),
    ("plot", "df_clean.plot()"),
    ("other", "x = 1"),
    ("summary", "stats = df_clean.describe()\nstats"),
    ("report", "print(stats)"),
]


def test_downstream_follows_derived_names() -> None:
    assert downstream(CELLS, 1, {"df"}) == ["clean", "plot", "summary", "report"]


def test_downstream_starts_at_start() -> None:
    assert downstream(CELLS, 2, {"df_clean"}) == ["plot", "summary", "report"]
    assert downstream(CELLS, 5, {"df_clean"}) == []
    assert downstream(CELLS, 99, {"df"}) == []


def test_downstream_with_nothing_tainted() -> None:
    assert downstream(CELLS, 0, set()) == []


def test_downstream_does_not_mutate_the_callers_set() -> None:
    tainted = {"df"}
    downstream(CELLS, 1, tainted)
    assert tainted == {"df"}


def test_rebinding_without_reading_starts_afresh() -> None:
    cells = [("a", "df_clean = pd.read_parquet(p)"), ("b", "df_clean.head()")]
    assert downstream(cells, 0, {"df_clean"}) == []


def test_conditional_rebinding_keeps_the_taint() -> None:
    cells = [("a", "if fast:\n    df_clean = small"), ("b", "df_clean.head()")]
    assert downstream(cells, 0, {"df_clean"}) == ["b"]


def test_mutation_carries_the_taint() -> None:
    cells = [
        ("a", "model.fit(X)"),
        ("b", "preds = model.predict(Z)"),
        ("c", "Z.shape"),
        ("d", "preds.mean()"),
    ]
    assert downstream(cells, 0, {"X"}) == ["a", "b", "d"]


@pytest.mark.parametrize("source", ["%%time\nprint(1)", "x = (", "%%bash\nls"])
def test_opaque_cells_count_as_using_everything(source: str) -> None:
    assert downstream([("a", source), ("b", "y = 1")], 0, {"anything"}) == ["a"]


# defined_names ----------------------------------------------------------------------------------
def test_defined_names_are_bindings_not_mutations() -> None:
    sources = ["import pandas as pd\ndf = pd.read_csv(p)", "df.dropna(inplace=True)\nmodel.fit(X)"]
    assert defined_names(sources) == {"pd", "df"}


@pytest.mark.parametrize(
    "source",
    [
        "from helpers import *",
        "%run helpers.py",
        "%store -r table",
        "exec(code)",
        "globals().update(d)",
        "x = (",
    ],
)
def test_defined_names_marks_unknown_definitions(source: str) -> None:
    assert EVERYTHING in defined_names(["a = 1", source])


def test_defined_names_of_cell_magics() -> None:
    assert defined_names(["%%capture out\nprint(1)", "%%bash\nls"]) == {"out"}


def test_analyze_is_cached_and_survives_deep_expressions() -> None:
    assert analyze("x = 1") is analyze("x = 1")
    flow = analyze("x = " + " + ".join(["a"] * 3000))  # may overflow the parser on 3.11
    assert flow.parsed is False or flow.uses == {"a"}
