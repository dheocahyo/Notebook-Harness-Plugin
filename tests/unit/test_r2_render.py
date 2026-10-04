"""Round-2 fixes for the self-check and 'check this' lines (verification items V6, V23, V25, X1)."""

from __future__ import annotations

import pytest

from nh_gateway._shared import secrets
from nh_gateway.render import headline, next_block, selfcheck
from tests.unit.test_render import PASSWORD, frame, payload, scalar

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


# design §6.8: a changed value is reported with both reprs redacted
@pytest.fixture
def installed(tmp_path):
    (tmp_path / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(tmp_path, {}))


def test_a_changed_secret_value_is_redacted_on_both_sides(installed):
    before = payload(api=scalar("old-value", "str"), key=scalar(PASSWORD, "str"))
    after = payload(api=scalar(PASSWORD, "str"), key=scalar(PASSWORD + "-rotated", "str"))
    lines, _ = selfcheck(before, after, code="api = key; key = rotate(key)")
    assert "api: 'old-value' → '[redacted:DB_PASSWORD]'" in lines
    assert "key: '[redacted:DB_PASSWORD]' → '[redacted:DB_PASSWORD]-rotated'" in lines
    assert not any(PASSWORD[:6] in line for line in lines)


# design §6.3: the approved batch's machine line and next blocks
CFG = {
    "turn": {
        "max_code_cells": 1,
        "max_retries": 2,
        "max_waits": 2,
        "max_undos": 3,
        "max_revisions": 2,
    }
}
CELL = '"Total price by region" [5]'


def test_machine_line_counts_against_the_batch():
    from nh_gateway.policy.turn import TurnState
    from nh_gateway.tools.common import machine_line

    state = TurnState("s1", "p2", 0.0, claims=["nh-1", "nh-2"], batch_total=3)
    assert machine_line("nh-2", 5, state, CFG) == (
        "nh: cell=nh-2 exec=5 turn=2/3 batch retries=0/2 waits=0/2 undos=0/3"
    )
    assert machine_line("nh-2", 5, state, CFG, writer=True).endswith(
        " turn=2/3 batch retries=0/2 waits=0/2 undos=0/3 revisions=0/2"
    )
    for total in (None, 0):  # undecided, or decided: no batch
        plain = TurnState("s1", "p2", 0.0, claims=["nh-1"], batch_total=total)
        assert " turn=1/1 retries=" in machine_line("nh-1", 5, plain, CFG)


def _block(status, batch, check_this=False):
    return next_block(
        status, retries_left=2, waits_left=2, cell=CELL, batch=batch, check_this=check_this
    )


def test_next_block_goes_on_to_the_next_step():
    assert _block("ok", (2, 3, 0)) == (
        f"Step 2 of 3 of the approved batch ran OK. Give the user a short report on {CELL}: what "
        "it did and the real numbers, surprises first, named by title and [n]. Then write step 3 "
        "of the plan with nh_add_cell, without waiting for the user."
    )


def test_next_block_closes_the_batch_on_its_last_step():
    plain = next_block("ok", retries_left=2, waits_left=2, cell=CELL)
    assert plain.endswith("Do not write a second cell.")
    assert _block("ok", (3, 3, 0)) == (
        "That was step 3 of 3, the last of the approved batch. "
        + plain.replace("Do not write a second cell.", "Do not write another cell.")
    )


def test_next_block_stops_on_check_this():
    assert _block("ok", (2, 3, 2), check_this=True) == (
        f"The approved batch stops at step 2 of 3: {CELL} ran, but its result needs a look (see "
        "'check this'). Reply to the user about it: lead with the 'check this' finding, then "
        "what the cell did and the real numbers, and say which planned steps did not run. Write "
        "no other cell and don't change this one in this message; wait for the user."
    )


def test_next_block_stops_on_an_error_with_no_retry():
    text = _block("error", (2, 3, 2))
    assert text == (
        f"The approved batch stops at step 2 of 3: {CELL} failed. Don't fix it in this message: "
        "a batch has no retries. Explain in plain words: quote the failing code, what Python "
        "said, the likely cause and one fix; say which planned steps did not run; then wait."
    )
    assert "nh_edit_cell" not in text


@pytest.mark.parametrize("status", ["running", "queued", "aborted", "timeout", "interrupted"])
def test_next_block_stops_on_any_other_status(status):
    plain = next_block(status, retries_left=2, waits_left=2, cell=CELL)
    assert _block(status, (2, 3, 2)) == (
        f"{plain} The approved batch stops at step 2 of 3: say which planned steps did not run."
    )


def test_next_block_after_an_earlier_stop():
    assert _block("ok", (2, 3, 2)) == (  # a wait for the step that stopped it, now OK
        f"{CELL} ran OK, but the approved batch stopped at step 2 of 3. Report the batch to the "
        "user: what each step did, then where and why it stopped. Write no other cell; wait "
        "for the user."
    )
    assert _block("ok", (1, 3, 2)).startswith(f"{CELL} ran OK, but the approved batch stopped")
    plain = next_block("lost", retries_left=2, waits_left=2, cell=CELL)
    assert _block("lost", (1, 3, 2)) == (
        f"{plain} The approved batch stopped at step 2 of 3: say which planned steps did not run."
    )


def test_next_block_without_a_batch_is_unchanged():
    for status in ("ok", "error", "running", "lost"):
        plain = next_block(status, retries_left=1, waits_left=1, cell=CELL)
        assert "batch" not in plain
        assert plain == next_block(
            status, retries_left=1, waits_left=1, cell=CELL, batch=None, check_this=True
        )


def test_the_writers_next_block_ignores_the_batch():
    writer = next_block("ok", retries_left=2, waits_left=2, cell=CELL, audience="writer")
    assert writer == next_block(
        "ok", retries_left=2, waits_left=2, cell=CELL, audience="writer", batch=(1, 3, 0)
    )
