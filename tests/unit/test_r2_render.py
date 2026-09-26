"""Round-2 fixes for the self-check and 'check this' lines (verification items V6, V23, V25, X1)."""

from __future__ import annotations

from nh_gateway.render import headline, next_block, selfcheck
from tests.unit.test_render import frame, payload

SALES = ["price", "qty", "region"]


def series(length, nulls=0):
    return {"kind": "Series", "lib": "pandas", "len": length, "dtype": "bool", "nulls": nulls}


# V6: a new frame filtered through a named mask or a comparison method loses rows visibly
def test_named_mask_filter_is_checked():
    before = payload(df=frame(10, columns=SALES, nulls={}))
    after = payload(
        df=frame(10, columns=SALES, nulls={}),
        is_expensive=series(10),
        df_exp=frame(3, columns=SALES, nulls={}),
    )
    code = 'is_expensive = df["price"] > 50\ndf_exp = df[is_expensive]'
    _, check = selfcheck(before, after, code=code)
    assert check == ["df_exp kept 3 of the 10 rows in df (70% removed)."]


def test_comparison_method_filter_is_checked():
    before = payload(df=frame(10, columns=SALES, nulls={}))
    after = payload(df=frame(10, columns=SALES, nulls={}), df_exp=frame(3, columns=SALES, nulls={}))
    _, check = selfcheck(before, after, code='df_exp = df.loc[df["price"].gt(50)]')
    assert check == ["df_exp kept 3 of the 10 rows in df (70% removed)."]


# X1: picking a column out for a look is not rows lost
def test_display_only_selection_is_not_rows_removed():
    before = payload(df=frame(100, columns=SALES, nulls={}), mask=series(100))
    after = payload(
        df=frame(100, columns=SALES, nulls={}),
        mask=series(100),
        unparsed={"kind": "Series", "lib": "pandas", "len": 2, "dtype": "object", "nulls": 0},
    )
    _, check = selfcheck(before, after, code='unparsed = df.loc[mask, "region"]')
    assert check == []


def test_column_list_is_not_a_mask():
    before = payload(df=frame(100, columns=SALES, nulls={}))
    after = payload(
        df=frame(100, columns=SALES, nulls={}),
        cols={"kind": "container", "type": "list", "len": 2},
        small=frame(100, columns=["price", "qty"], nulls={}),
    )
    _, check = selfcheck(before, after, code='cols = ["price", "qty"]\nsmall = df[cols]')
    assert check == []


# V23: merge growth is measured against the left frame, even when the lookup is bigger
def test_merge_fan_out_with_a_larger_lookup_table():
    before = payload(
        df_revenue=frame(4, columns=["customer_id", "revenue"], nulls={}),
        customers=frame(9, columns=["customer_id", "segment"], nulls={}),
    )
    after = payload(
        df_revenue=frame(4, columns=["customer_id", "revenue"], nulls={}),
        customers=frame(9, columns=["customer_id", "segment"], nulls={}),
        df_segmented=frame(6, columns=["customer_id", "revenue", "segment"], nulls={}),
    )
    code = 'df_segmented = df_revenue.merge(customers, on="customer_id", how="left")'
    lines, check = selfcheck(before, after, code=code)
    assert check == [
        "df_segmented has 6 rows, more than df_revenue (4): the merge/join may have duplicated rows."
    ]
    assert headline(lines).startswith("df_segmented: new DataFrame 6×3")


def test_headline_prefers_the_frame_the_cell_shows():
    before = payload()
    after = payload(
        customers=frame(9, columns=["customer_id", "segment"], nulls={}),
        orders=frame(4, columns=["customer_id", "revenue"], nulls={}),
    )
    code = "customers = load_customers()\norders = load_orders()\norders.head()"
    lines, _ = selfcheck(before, after, code=code)
    assert headline(lines).startswith("orders:")


# V25: 'same shape and nulls' is not 'unchanged', and changed values are said
def test_same_shape_is_not_called_unchanged():
    before = payload(df=frame(10, columns=SALES, nulls={}))
    after = payload(df=frame(10, columns=SALES, nulls={}))
    lines, _ = selfcheck(before, after, code="df.head()")
    assert lines == ["same shape and nulls: df"]


def test_changed_values_are_reported():
    before = payload(by_region=dict(frame(4, columns=["total"], nulls={}), sums="aaaa0000"))
    after = payload(by_region=dict(frame(4, columns=["total"], nulls={}), sums="bbbb1111"))
    lines, _ = selfcheck(before, after, code='by_region = df.groupby("region").median()')
    assert lines == ["by_region: DataFrame 4×1; values changed"]


# V26: an untitled cell opens a sentence with a capital letter
def test_next_block_capitalises_an_untitled_label():
    text = next_block("error", retries_left=1, waits_left=2, cell="the cell `x = y` [3]")
    assert text.startswith("The cell `x = y` [3] failed.")
