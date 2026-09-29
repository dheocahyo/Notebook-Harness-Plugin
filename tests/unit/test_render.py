"""Result text: cell labels, the before/after self-check, 'check this', the reply contract."""

from __future__ import annotations

import pytest

from nh_gateway import render
from nh_gateway._shared import secrets
from nh_gateway.policy.turn import TurnState
from nh_gateway.render import (
    Result,
    cell_label,
    error_summary,
    headline,
    next_block,
    selfcheck,
)
from nh_gateway.tools.common import machine_line


def frame(rows, cols=None, *, nulls=None, columns=None, dtypes=None, lib="pandas"):
    columns = columns if columns is not None else [f"c{i}" for i in range(cols or 0)]
    value = {
        "kind": "DataFrame",
        "lib": lib,
        "shape": [rows, len(columns)],
        "columns": columns,
    }
    if nulls is not None:
        value["nulls"] = nulls
    if dtypes is not None:
        value["dtypes"] = dtypes
    return value


def payload(truncated=False, **variables):
    return {"vars": variables, "truncated": truncated, "packages": {}}


SALES = ["price", "qty", "region", "date", "store", "sku", "promo", "channel"]


# --- cell_label -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "count", "source", "label"),
    [
        ("Drop rows with missing price", 14, "", '"Drop rows with missing price" [14]'),
        (
            "### Drop rows  with\nmissing price",
            14,
            "",
            '"Drop rows with missing price" [14]',
        ),
        ("Load raw data", None, "", '"Load raw data"'),
        (
            None,
            3,
            "\n\n  df = pd.read_csv(DATA_PATH)\nprint(df)",
            "the cell `df = pd.read_csv(DATA_PATH)` [3]",
        ),
        (
            "",
            5,
            "summary = df.groupby('region').agg(total=('price', 'sum')).reset_index()",
            "the cell `summary = df.groupby('region').agg(tota…` [5]",
        ),
        (None, 7, "   \n", "an empty cell [7]"),
    ],
)
def test_cell_label(title, count, source, label):
    assert cell_label(title, count, source) == label


def test_cell_label_fallback_is_at_most_40_chars():
    name = cell_label(None, None, "x" * 100).removeprefix("the cell ").strip("`")
    assert len(name) == 40 and name.endswith("…")


def test_cell_label_shows_an_escaped_note_title_as_typed():
    # The note heading escapes $ and ~ for JupyterLab; the label reads it back without them.
    assert cell_label(r"### Revenue in \$ (\~5% cut)", 3) == '"Revenue in $ (~5% cut)" [3]'


# --- self-check -----------------------------------------------------------------------------


def test_frame_diff_shows_shape_rows_and_nulls():
    before = payload(df=frame(10432, columns=SALES, nulls={"price": 312, "qty": 4}))
    after = payload(df=frame(10120, columns=SALES, nulls={"qty": 4}))
    lines, check = selfcheck(before, after, code="df = df.dropna(subset=['price'])")
    assert lines == ["df: DataFrame 10,432×8 → 10,120×8 (-312 rows); nulls price 312 → 0"]
    assert check == []


def test_new_removed_and_unchanged_names():
    before = payload(
        df=frame(100, 3, nulls={}),
        tmp={"kind": "scalar", "type": "int", "repr": "1"},
        n={"kind": "scalar", "type": "int", "repr": "42"},
    )
    after = payload(
        df=frame(100, 3, nulls={}),
        n={"kind": "scalar", "type": "int", "repr": "42"},
        X={"kind": "ndarray", "shape": [800, 12], "dtype": "float64"},
        s={
            "kind": "Series",
            "lib": "pandas",
            "len": 10,
            "dtype": "int64",
            "nulls": 2,
            "name": "x",
        },
        model={"kind": "object", "type": "sklearn.linear_model._base.LinearRegression"},
        rows={"kind": "container", "type": "list", "len": 3},
    )
    lines, _ = selfcheck(before, after)
    assert lines == [
        "s: new Series len 10 int64; nulls 2",
        "X: new ndarray (800, 12) float64",
        "model: new LinearRegression",
        "rows: new list len 3",
        "removed: tmp",
        "same shape and nulls: df; unchanged: n",
    ]


def scalar(value, type_="int"):
    return {"kind": "scalar", "type": type_, "repr": repr(value)}


def test_new_frames_come_before_constants_and_plots():
    # Namespace order puts the UPPER_CASE constants first; the data change must lead anyway.
    before = payload(df=frame(10, 1, nulls={}))
    after = payload(
        df=frame(10, 1, nulls={}),
        DATA_PATH=scalar("../data/raw/sales.csv", "str"),
        MIN_UNITS=scalar(1),
        ax={"kind": "object", "type": "matplotlib.axes._axes.Axes"},
        by_region={"kind": "Series", "len": 4, "dtype": "float64", "nulls": 0},
        df_clean=frame(8, 1, nulls={}),
    )
    code = "MIN_UNITS = 1\ndf_clean = df[df.units >= MIN_UNITS]\nby_region = df_clean.price"
    lines, _ = selfcheck(before, after, code=code)
    assert lines[:2] == [
        "df_clean: new DataFrame 8×1 (from df 10×1); no nulls",
        "by_region: new Series len 4 float64 (from df_clean 8×1)",
    ]
    assert lines[-1] == "same shape and nulls: df"
    assert headline(lines) == "df_clean: new DataFrame 8×1 (from df 10×1); no nulls"


def test_changed_frames_and_series_qualify_as_headline():
    before = payload(df=frame(10432, columns=SALES), s={"kind": "Series", "len": 10})
    after = payload(df=frame(10120, columns=SALES), s={"kind": "Series", "len": 8})
    lines, _ = selfcheck(before, after, code="df = df.dropna()")
    assert headline(lines) == "df: DataFrame 10,432×8 → 10,120×8 (-312 rows)"
    assert headline(["x: 42 → 43", "s: Series len 10 → 8 (-2)"]) == "s: Series len 10 → 8 (-2)"
    assert headline(["X: new ndarray (800, 12) float64"]) == "X: new ndarray (800, 12) float64"


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["PRICE_FLOOR: new int 50"],
        ["intercept: new numpy.float64 np.float64(6.082926829268295)", "ax: new Axes"],
        ["fig: new Figure", "model: new LinearRegression"],
        ["summary unchanged: df, price_by_region"],
        ["removed: tmp"],
        ["self-check skipped (TimeoutError: probe over budget)"],
        ["… 6 more changed or new names"],
        ["x: int 5 → str 'a'"],
    ],
)
def test_headline_is_never_a_constant_plot_or_unchanged_line(lines):
    assert headline(lines) is None


def test_headline_skips_the_unknown_state_line_and_is_one_short_line():
    lines, _ = selfcheck({"error": "kernel busy"}, payload(n=scalar(1), df=frame(3, 2, nulls={})))
    assert lines[0].startswith("state before the run unknown")
    assert headline(lines) == "df: DataFrame 3×2; no nulls"
    long = "df: DataFrame 3×2; nulls: " + ", ".join(f"column_{i} 1" for i in range(30))
    short = headline([long])
    assert short is not None and len(short) <= 100 and short.endswith("…")
    assert "\n" not in short and short[:-1] == long[: len(short) - 1].rstrip(" ,;:-")


def test_column_dtype_and_value_changes():
    before = payload(
        df=frame(10, columns=["a", "b"], dtypes={"a": "object", "b": "int64"}),
        s={"kind": "Series", "len": 10, "dtype": "int64", "nulls": 3},
        n={"kind": "scalar", "type": "int", "repr": "42"},
        items={"kind": "container", "type": "list", "len": 3},
        model={"kind": "object", "type": "a.LinearRegression"},
    )
    after = payload(
        df=frame(10, columns=["a", "c"], dtypes={"a": "float64", "c": "int64"}),
        s={"kind": "Series", "len": 8, "dtype": "float64", "nulls": 0},
        n={"kind": "scalar", "type": "int", "repr": "43"},
        items={"kind": "container", "type": "list", "len": 4},
        model={"kind": "object", "type": "b.Pipeline"},
    )
    lines, _ = selfcheck(before, after)
    assert lines == [
        "df: DataFrame 10×2; new columns c; dropped columns b; dtype a object → float64",
        "s: Series len 10 → 8 (-2); dtype int64 → float64; nulls 3 → 0",
        "n: 42 → 43",
        "items: list len 3 → list len 4",
        "model: LinearRegression → Pipeline",
    ]


def test_kind_change_and_polars_frames():
    before = payload(x=frame(10, 2))
    after = payload(x={"kind": "Series", "lib": "polars", "len": 10, "dtype": "i64"})
    assert selfcheck(before, after)[0] == ["x: DataFrame 10×2 → Series (polars) len 10 i64"]


def test_cut_column_lists_do_not_invent_column_changes():
    before = payload(wide={"kind": "DataFrame", "shape": [5, 300], "columns": ["a", "b"]})
    after = payload(wide={"kind": "DataFrame", "shape": [5, 300], "columns": ["a", "z"]})
    assert selfcheck(before, after)[0] == ["same shape: wide"]  # no null counts to compare


def test_self_check_is_capped_at_eight_lines():
    before = payload(old=frame(1, 1))
    after = payload(**{f"v{i}": frame(i + 1, 2) for i in range(12)})
    lines, _ = selfcheck(before, after)
    assert len(lines) == 8
    assert lines[-2] == "… 6 more changed or new names"
    assert lines[-1] == "removed: old"


def test_truncated_payloads_do_not_claim_new_or_removed():
    before = payload(truncated=True, a=frame(5, 1))
    after = payload(truncated=True, b=frame(5, 1))
    lines, _ = selfcheck(before, after)
    assert lines == [
        "b: DataFrame 5×1",
        "(more variables than the probe lists; the rest are not shown)",
    ]


def test_probe_errors_and_missing_payloads():
    assert selfcheck({}, {}) == ([], [])
    assert selfcheck(payload(), {"error": "TimeoutError: probe over budget"}) == (
        ["self-check skipped (TimeoutError: probe over budget)"],
        [],
    )
    lines, _ = selfcheck({"error": "kernel busy"}, payload(df=frame(3, 2, nulls={})))
    assert lines == [
        "state before the run unknown (kernel busy); after it:",
        "df: DataFrame 3×2; no nulls",
    ]
    lines, _ = selfcheck({}, payload(df=frame(3, 2)))
    assert lines[0] == "state before the run unknown (not taken); after it:"


# --- check this -----------------------------------------------------------------------------


def check(before_vars, after_vars, code):
    return selfcheck(payload(**before_vars), payload(**after_vars), code=code)[1]


def test_rows_went_to_zero():
    notes = check({"df": frame(10432, 8)}, {"df": frame(0, 8)}, "df = df[df['price'] > 1e9]")
    assert notes == ["df now has 0 rows (was 10,432)."]


def test_more_than_half_the_rows_removed():
    notes = check({"df": frame(10432, 8)}, {"df": frame(3964, 8)}, "df = df.dropna()")
    assert notes == ["df lost 62% of its rows (10,432 → 3,964)."]


def test_half_or_less_removed_is_not_a_surprise():
    assert check({"df": frame(100, 2)}, {"df": frame(50, 2)}, "df = df.dropna()") == []


def test_rows_grew_after_merge():
    notes = check(
        {"df": frame(10432, 8)},
        {"df": frame(12004, 9)},
        "df = df.merge(stores, on='store')",
    )
    assert notes == [
        "df grew from 10,432 to 12,004 rows after a merge/join; duplicate keys may have multiplied rows."
    ]
    assert check({"df": frame(10, 2)}, {"df": frame(20, 2)}, "df = pd.concat([df, df])") == []


def test_new_frame_bigger_than_its_merge_inputs():
    before = {"df": frame(10432, 8), "stores": frame(50, 3)}
    code = "merged = (\n    df\n    .merge(stores, on='store', how='left')\n)"
    notes = check(before, {**before, "merged": frame(12004, 10)}, code)
    assert notes == [
        "merged has 12,004 rows, more than df (10,432): the merge/join may have duplicated rows."
    ]
    assert check(before, {**before, "merged": frame(10432, 10)}, code) == []


def test_new_all_null_column():
    before = {"df": frame(100, columns=["a", "old_empty"], nulls={"old_empty": 100})}
    after = {
        "df": frame(
            100,
            columns=["a", "old_empty", "region"],
            nulls={"old_empty": 100, "region": 100},
        )
    }
    assert check(before, after, "df['region'] = df['a'].map(REGIONS)") == [
        "df: new column region is entirely null."
    ]


def test_all_null_columns_of_a_new_frame():
    before = {"df": frame(100, 2)}
    after = {
        **before,
        "joined": frame(100, columns=["a", "x", "y"], nulls={"x": 100, "y": 100, "a": 3}),
    }
    assert check(before, after, "joined = df.merge(lookup, how='left')") == [
        "joined: columns x, y are entirely null."
    ]


@pytest.mark.parametrize(
    "code",
    [
        "df = df.dropna()",
        "df.drop_duplicates(inplace=True)",
        "df = df[df['price'] > 0]",
        "df = df.loc[df.region.isin(REGIONS)]",
        "%matplotlib inline\ndf = df[~df.sku.str.startswith('X')]",
        "df = df.dropna(\n",  # unparsable: the line-based fallback still sees it
    ],
)
def test_shape_unchanged_after_a_drop_or_filter(code):
    notes = check({"df": frame(10432, 8)}, {"df": frame(10432, 8)}, code)
    assert notes == [
        "df still has 10,432 rows × 8 columns after the drop/filter: nothing was removed."
    ]


def test_new_frame_same_size_as_its_source_after_a_drop():
    before = {"df": frame(10432, 8)}
    notes = check(before, {**before, "df_clean": frame(10432, 8)}, "df_clean = df.dropna()")
    assert notes == [
        "df_clean has the same 10,432 rows × 8 columns as df: the drop/filter removed nothing."
    ]


def test_new_frame_empty_after_a_filter():
    before = {"df": frame(10432, 8)}
    notes = check(before, {**before, "north": frame(0, 8)}, "north = df[df.region == 'north']")
    assert notes == ["north is empty: 0 rows (from df, which has 10,432)."]


def test_new_name_that_keeps_under_half_of_its_source():
    # Review finding 13: the skill's own style (new names, constants) must get the >50% check.
    before = payload(df=frame(10, columns=["price"], nulls={}))
    after = payload(
        df=frame(10, columns=["price"], nulls={}),
        PRICE_FLOOR=scalar(50),
        df_expensive=frame(3, columns=["price"], nulls={}),
    )
    code = 'PRICE_FLOOR = 50\ndf_expensive = df[df["price"] > PRICE_FLOOR]\ndf_expensive.shape'
    lines, notes = selfcheck(before, after, code=code)
    assert notes == ["df_expensive kept 3 of the 10 rows in df (70% removed)."]
    assert lines == [
        "df_expensive: new DataFrame 3×1 (from df 10×1); no nulls",
        "PRICE_FLOOR: new int 50",
        "same shape and nulls: df",
    ]


@pytest.mark.parametrize(
    "code",
    [
        "df_clean = df.dropna()",
        "df_clean = df.drop_duplicates(subset=['sku'])",
        "df_clean = df.query('price > 100')",
        "df_clean = df[df.price > df.price.mean()]",  # a reduction inside the mask
        "df_clean = df.loc[df.region.isin(REGIONS), ['price', 'qty']]",
        "df_clean = (\n    df\n    .dropna(subset=['price'])\n    .reset_index(drop=True)\n)",
    ],
)
def test_new_name_row_loss_after_drop_or_filter(code):
    before = {"df": frame(10432, 8)}
    notes = check(before, {**before, "df_clean": frame(3964, 8)}, code)
    assert notes == ["df_clean kept 3,964 of the 10,432 rows in df (62% removed)."]


def test_new_name_row_loss_counts_a_source_made_in_the_same_cell():
    after = {"df": frame(10, 2), "df_small": frame(2, 2)}
    code = "df = pd.read_csv(DATA_PATH)\ndf_small = df[df.qty > 1]"
    lines, notes = selfcheck(payload(), payload(**after), code=code)
    assert notes == ["df_small kept 2 of the 10 rows in df (80% removed)."]
    assert "df_small: new DataFrame 2×2 (from df 10×2)" in lines


@pytest.mark.parametrize(
    ("code", "rows"),
    [
        ("by_region = df[df.price > 0].groupby('region')['price'].median()", 4),
        ("counts = df[df.price > 0]['region'].value_counts()", 4),
        ("top = df[df.price > 0].nlargest(10, 'price')", 10),
        ("peek = df.dropna().head()", 5),
        ("sample = df[df.qty > 1].sample(100)", 100),
        ("means = df[df.qty > 1].mean(numeric_only=True)", 8),
        ("small = df.dropna()", 6000),  # under half removed
        ("scaled = df.assign(price=df.price / 100).iloc[:10]", 10),  # no drop/filter
    ],
)
def test_no_row_loss_note_for_aggregates_explicit_subsets_or_small_losses(code, rows):
    before = {"df": frame(10432, 8)}
    name = code.split(" =", 1)[0]
    after = {
        **before,
        name: frame(rows, 2) if "means" not in code else {"kind": "Series", "len": rows},
    }
    assert check(before, after, code) == []


def test_a_frame_named_only_inside_a_string_is_not_a_source():
    before = {"df": frame(10, 2)}
    code = "orders = pd.read_sql('select * from df where qty > 1', con)"
    lines, notes = selfcheck(payload(**before), payload(**before, orders=frame(2, 2)), code=code)
    assert notes == []
    assert lines[0] == "orders: new DataFrame 2×2"


def test_new_name_row_loss_uses_the_frame_being_filtered():
    before = {"df": frame(10432, 8), "stores": frame(50, 3)}
    code = "open_stores = stores[stores.store_id.isin(df.store_id)]"
    assert check(before, {**before, "open_stores": frame(30, 3)}, code) == []
    assert check(before, {**before, "open_stores": frame(20, 3)}, code) == [
        "open_stores kept 20 of the 50 rows in stores (60% removed)."
    ]


def test_derived_frames_do_not_repeat_a_source_all_null_column():
    # Review finding 63: the load cell reports `comment`; frames made from it must not repeat it.
    columns = ["price", "comment"]
    before = {"df": frame(100, columns=columns, nulls={"comment": 100, "price": 4})}
    df_clean = frame(96, columns=columns, nulls={"comment": 96})
    assert (
        check(
            before,
            {**before, "df_clean": df_clean},
            "df_clean = df.dropna(subset=['price'])",
        )
        == []
    )
    before = {**before, "df_clean": df_clean}
    df_revenue = frame(
        96,
        columns=[*columns, "revenue", "discount"],
        nulls={"comment": 96, "discount": 96},
    )
    notes = check(
        before,
        {**before, "df_revenue": df_revenue},
        "df_revenue = df_clean.assign(revenue=df_clean.price * 2, discount=DISCOUNTS)",
    )
    assert notes == ["df_revenue: column discount is entirely null."]


@pytest.mark.parametrize(
    ("before", "after", "code"),
    [
        ({"df": frame(10, 2)}, {"df": frame(10, 3)}, "df['x'] = 1"),
        ({"df": frame(10, 2)}, {"df": frame(10, 2)}, "df.head()"),
        ({"df": frame(10, 2)}, {"df": frame(10, 2)}, "df_clean = df.dropna()\ndf"),
        ({}, {"results": frame(0, 0)}, "results = pd.DataFrame()"),
        (
            {"df": frame(10, 2)},
            {
                "df": frame(10, 2),
                "cols": {"kind": "container", "type": "list", "len": 2},
            },
            "cols = [c for c in df.columns if df[c].dtype == 'O']",
        ),
    ],
)
def test_no_false_surprises(before, after, code):
    assert check(before, after, code) == []


def test_unknown_before_state_still_flags_null_columns_only():
    after = payload(
        df=frame(0, columns=["a"], nulls={}),
        j=frame(5, columns=["a", "b"], nulls={"b": 5}),
    )
    assert selfcheck({"error": "kernel busy"}, after, code="df = df[df.a > 1]")[1] == [
        "j: column b is entirely null."
    ]


# --- next block -----------------------------------------------------------------------------

CELL = '"Drop rows with missing price" [14]'


@pytest.mark.parametrize(
    "status",
    [
        "ok",
        "error",
        "aborted",
        "running",
        "queued",
        "timeout",
        "interrupted",
        "deleted",
        "lost",
    ],
)
@pytest.mark.parametrize(("retries", "waits"), [(0, 0), (1, 1), (2, 2)])
def test_next_block_names_the_cell_by_label_only(status, retries, waits):
    text = next_block(status, retries_left=retries, waits_left=waits, cell=CELL)
    assert "nh-" not in text and "line " not in text
    assert CELL in text


def test_next_block_ok_is_the_reply_contract():
    text = next_block("ok", retries_left=2, waits_left=2, cell=CELL)
    for part in (
        "what the cell does",
        "why this approach",
        "judgment calls",
        "real numbers",
        "surprises first",
        "failed attempts",
        '"go"',
        "title and [n]",
        "Do not write a second cell",
    ):
        assert part in text


def test_next_block_error_with_and_without_retries():
    assert "nh_edit_cell" in next_block("error", retries_left=2, waits_left=0, cell=CELL)
    assert "(2 retries left" in next_block("error", retries_left=2, waits_left=0, cell=CELL)
    assert "(1 retry left" in next_block("error", retries_left=1, waits_left=0, cell=CELL)
    final = next_block("error", retries_left=0, waits_left=0, cell=CELL)
    assert "nh_edit_cell" not in final
    for part in (
        "no retries are left",
        "quote the failing code",
        "what Python said",
        "likely cause",
        "one fix",
        "undo",
    ):
        assert part in final


def test_next_block_running_counts_waits():
    text = next_block("running", retries_left=2, waits_left=1, cell=CELL)
    assert 'nh_run(mode="wait")' in text and "(1 wait left)" in text
    last = next_block("running", retries_left=2, waits_left=0, cell=CELL)
    assert "nh_run" not in last and "No waits are left" in last


def test_next_block_queued_says_nothing_ran_yet():
    text = next_block("queued", retries_left=2, waits_left=1, cell=CELL)
    assert "has not started" in text and "behind a cell already running" in text
    assert "still running" not in text and "outputs keep appearing" not in text
    assert 'nh_run(mode="wait")' in text and "(1 wait left)" in text
    assert "stop the running cell in JupyterLab" in text and "Do not write another cell" in text
    assert "No waits are left" in next_block("queued", retries_left=2, waits_left=0, cell=CELL)


def test_next_block_interrupted_is_not_a_failure_to_fix():
    text = next_block("interrupted", retries_left=2, waits_left=2, cell=CELL)
    assert "stopped before it finished" in text and "not a failure to fix" in text
    assert "Do not re-run or edit it without asking" in text
    assert "nh_edit_cell" not in text and "retries" not in text


def test_next_block_deleted_forbids_rebuilding_the_cell():
    text = next_block("deleted", retries_left=2, waits_left=2, cell=CELL)
    assert "The user deleted" in text and "take that as a no" in text
    assert "Do not rebuild, re-add or re-run it" in text
    assert "nh_edit_cell" not in text and "nh_add_cell" not in text


def test_next_block_other_statuses():
    assert "The cell ahead of yours failed; nothing of yours ran" in next_block(
        "aborted", retries_left=2, waits_left=2, cell=CELL
    )
    assert "used no retry" in next_block("aborted", retries_left=2, waits_left=2, cell=CELL)
    assert "hard_timeout_s" in next_block("timeout", retries_left=0, waits_left=0, cell=CELL)
    assert "interrupted" in next_block("interrupted", retries_left=0, waits_left=0, cell=CELL)
    assert "Kernel → Restart Kernel and Run Up to Selected Cell" in next_block(
        "lost", retries_left=0, waits_left=0, cell=CELL
    )
    assert next_block("weird", retries_left=0, waits_left=0, cell=CELL).startswith("Tell the user")


# nh:cell-writer's next block: its final answer goes to the nh:qa-cell workflow, never the user.

STATUSES = [
    "ok",
    "error",
    "aborted",
    "running",
    "queued",
    "timeout",
    "interrupted",
    "deleted",
    "lost",
    "weird",
]


@pytest.mark.parametrize("status", STATUSES)
@pytest.mark.parametrize(("retries", "waits"), [(0, 0), (1, 1), (2, 2)])
def test_writer_next_block_returns_the_result_to_the_workflow(status, retries, waits):
    text = next_block(status, retries_left=retries, waits_left=waits, cell=CELL, audience="writer")
    assert "return this whole result to the workflow" in text.lower()
    assert f"(status {status})" in text
    assert "Reply to the user" not in text and "Tell the user" not in text
    assert "undo" not in text.lower()
    assert CELL in text and "nh-" not in text


def test_writer_next_block_fixes_and_waits_only_within_the_budget():
    def writer(status, retries=0, waits=0):
        return next_block(
            status, retries_left=retries, waits_left=waits, cell=CELL, audience="writer"
        )

    assert "nh_edit_cell on the same cell (1 retry left" in writer("error", retries=1)
    assert "nh_edit_cell" not in writer("error") and "no retries are left" in writer("error")
    for status in ("running", "queued"):
        assert 'nh_run(mode="wait") on it (1 wait left)' in writer(status, waits=1)
        assert "nh_run" not in writer(status) and "No waits are left" in writer(status)
    for status in ("ok", "aborted", "timeout", "interrupted", "deleted", "lost", "weird"):
        text = writer(status, retries=2, waits=2)
        assert "nh_edit_cell" not in text and "nh_run" not in text, status
    assert "Don't reply to the user or write a second cell" in writer("ok")


def test_the_machine_line_counts_revisions_for_the_writer_only():
    cfg = {
        "turn": {
            "max_code_cells": 1,
            "max_retries": 2,
            "max_waits": 2,
            "max_undos": 3,
            "max_revisions": 2,
        }
    }
    state = TurnState("s1", "p1", 0.0, claims=["nh-4f2a91c07b"], revisions={"nh-4f2a91c07b": 1})
    main = machine_line("nh-4f2a91c07b", 14, state, cfg)
    assert main == "nh: cell=nh-4f2a91c07b exec=14 turn=1/1 retries=0/2 waits=0/2 undos=0/3"
    assert machine_line("nh-4f2a91c07b", 14, state, cfg, writer=True) == main + " revisions=1/2"
    fresh = TurnState("s1", "p1", 0.0, claims=["nh-4f2a91c07b"])
    assert machine_line("nh-4f2a91c07b", None, fresh, cfg, writer=True).endswith(
        " exec=- turn=1/1 retries=0/2 waits=0/2 undos=0/3 revisions=0/2"
    )


# --- Result ---------------------------------------------------------------------------------


def test_result_text_joins_sections_after_the_two_header_lines():
    result = (
        Result(
            'Added "Drop rows with missing price" as [14] at the bottom; ran ok in 0.8s.',
            "nh: cell=nh-4f2a91c07b exec=14 turn=1/1 retries=0/2 waits=0/2 undos=0/3",
        )
        .section("output", "[stdout] Dropped 312 rows")
        .section("self-check", ["df: DataFrame 10,432×8 → 10,120×8 (-312 rows)"])
        .section("next", "Reply to the user.")
    )
    assert result.text() == (
        'Added "Drop rows with missing price" as [14] at the bottom; ran ok in 0.8s.\n'
        "nh: cell=nh-4f2a91c07b exec=14 turn=1/1 retries=0/2 waits=0/2 undos=0/3\n"
        "--- output ---\n[stdout] Dropped 312 rows\n"
        "--- self-check ---\ndf: DataFrame 10,432×8 → 10,120×8 (-312 rows)\n"
        "--- next ---\nReply to the user."
    )


def test_result_skips_empty_sections_and_keeps_plan_order():
    result = Result("first", "nh: machine")
    result.section("next", "stop").section("stale", []).section("kernel", "").section(
        "config", ["", ""]
    )
    result.section("readability hints", ['- "df2 = df" uses a cryptic name'])
    result.section("extra", "custom").section("check this", ["df now has 0 rows (was 10)."])
    result.section("output", "(no output)\n")
    assert result.text() == (
        "first\nnh: machine\n"
        "--- check this ---\ndf now has 0 rows (was 10).\n"
        "--- output ---\n(no output)\n"
        '--- readability hints (advisory) ---\n- "df2 = df" uses a cryptic name\n'
        "--- extra ---\ncustom\n"
        "--- next ---\nstop"
    )


def test_result_with_no_sections_is_just_the_header():
    assert Result("Not written.", "nh: E110").text() == "Not written.\nnh: E110"


def test_result_lead_lines_go_above_the_first_line():
    result = Result('Added "Keep expensive orders" [2]; ran ok in 0.1s.', "nh: cell=nh-1")
    result.section("next", "Reply.")
    result.lead("Kernel ≠ notebook: `df_revenue` still holds results from an undone cell.")
    result.lead(["", "NEW kernel: earlier variables are gone.\n"]).lead([])
    assert result.text() == (
        "Kernel ≠ notebook: `df_revenue` still holds results from an undone cell.\n"
        "NEW kernel: earlier variables are gone.\n"
        'Added "Keep expensive orders" [2]; ran ok in 0.1s.\n'
        "nh: cell=nh-1\n"
        "--- next ---\nReply."
    )


# --- error summary --------------------------------------------------------------------------

TO_DATETIME = (
    "day is out of range for month. You might want to try:\n"
    "    - passing `format` if your strings have a consistent format;\n"
    "    - passing `format='ISO8601'` if your strings are all ISO8601 but not necessarily in "
    "exactly the same format;\n"
    "    - passing `format='mixed'`, and the format will be inferred for each element "
    "individually. You might want to use `dayfirst` alongside this."
)


def test_error_summary_keeps_the_first_line_of_a_multi_line_message():
    # Review finding 51: the first line of a result must stay one line, cut at a word.
    assert error_summary("ValueError", TO_DATETIME) == (
        "ValueError: day is out of range for month. You might want to try…"
    )
    assert error_summary("KeyError", "'price'") == "KeyError: 'price'"
    assert error_summary("StopIteration", "") == "StopIteration"


def test_error_summary_cuts_long_messages_at_a_word_boundary():
    evalue = "could not convert string to float: " + "'abc def ghi' " * 20
    text = error_summary("ValueError", evalue, limit=60)
    assert len(text) <= 60 and text.endswith("…") and "\n" not in text
    assert text == "ValueError: could not convert string to float: 'abc def…"


# --- redaction (design §6.8): every label, summary and scalar is redacted before its cut ------

PASSWORD = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; under DB_PASSWORD in the project's .env


@pytest.fixture
def installed(tmp_path):
    (tmp_path / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(tmp_path, {}))


def test_labels_are_redacted_before_the_cut(installed):
    assert cell_label(f"Connect with {PASSWORD}", 3) == '"Connect with [redacted:DB_PASSWORD]" [3]'
    # The password starts at column 25 of a 40-char label: cut first, 14 of its chars would show.
    label = cell_label(None, 4, f"conn = connect(password='{PASSWORD}')")
    assert label == "the cell `conn = connect(password='[redacted:DB_P…` [4]"
    assert cell_label(None, 4, f"c = f('{PASSWORD}')") == (
        "the cell `c = f('[redacted:DB_PASSWORD]')` [4]"
    )


def test_error_summaries_are_redacted_before_the_cut(installed):
    assert error_summary("RuntimeError", f"x {PASSWORD}", 120) == (
        "RuntimeError: x [redacted:DB_PASSWORD]"
    )
    cut = error_summary("RuntimeError", f"login failed for user admin with {PASSWORD} on db", 60)
    assert cut == "RuntimeError: login failed for user admin with…"
    assert error_summary("E", f"{PASSWORD}\nmore", 120) == "E: [redacted:DB_PASSWORD]…"


def test_an_error_summary_cut_inside_a_long_word_shows_no_piece_of_the_secret(installed):
    """A URL is one word, so the cut can't fall back to a space: cut first, the password's first
    5 chars would show, too few for the cut-piece rule (review of C3)."""
    evalue = f"could not connect to postgresql+psycopg2://reporting_app:{PASSWORD}@db:5432/sales"
    assert ("OperationalError: " + evalue).index(PASSWORD) == 80 - 5
    assert error_summary("OperationalError", evalue, 81) == (
        "OperationalError: could not connect to postgresql+psycopg2://[redacted:DB_PASSWO…"
    )


def test_name_summaries_and_changes_are_redacted_whole(installed):
    """_describe and _diff build their lines from names nh doesn't cut (a type, a dtype): the
    whole line is redacted."""
    assert render._describe({"kind": "container", "type": f"dict_{PASSWORD}", "len": 2}) == (
        "dict_[redacted:DB_PASSWORD] len 2"
    )
    old = {"kind": "Series", "rows": 3, "dtype": "int64"}
    new = {"kind": "Series", "rows": 3, "dtype": f"category_{PASSWORD}"}
    assert render._diff(old, new) == "Series dtype int64 → category_[redacted:DB_PASSWORD]"


def test_scalar_reprs_in_the_self_check_are_redacted(installed):
    lines, _ = selfcheck(payload(), payload(api=scalar(PASSWORD, "str")))
    assert lines == ["api: new str '[redacted:DB_PASSWORD]'"]
    lines, _ = selfcheck({"error": f"probe failed: {PASSWORD}"}, payload(n=scalar(1)))
    assert (
        lines[0] == "state before the run unknown (probe failed: [redacted:DB_PASSWORD]); after it:"
    )
