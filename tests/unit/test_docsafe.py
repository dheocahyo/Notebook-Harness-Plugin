"""exec/docsafe: values the CRDT document accepts, and output caps."""

from __future__ import annotations

import math

import pycrdt
import pytest
from hypothesis import given
from hypothesis import strategies as st

from nh_gateway.backend.kernel import output_hook
from nh_gateway.exec import docsafe

json_like = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats() | st.text(),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)


def test_sanitize_replaces_values_pycrdt_and_yjs_reject():
    value = {
        "nan": math.nan,
        "inf": [math.inf, -math.inf, 1.5],
        "big": 2**70,
        "js_unsafe": 2**53,
        "ok_int": 2**53 - 1,
        "flag": True,
        "text": "a\udc80b",
        "pair": "\U0001f600",
        ("tuple", 1): (1, 2),
        "\udcff": "key had a lone surrogate",
    }
    clean = docsafe.sanitize_for_doc(value)
    assert clean["nan"] is None
    assert clean["inf"] == [None, None, 1.5]
    assert clean["big"] == str(2**70)
    assert clean["js_unsafe"] == str(2**53)
    assert clean["ok_int"] == 2**53 - 1
    assert clean["flag"] is True
    assert clean["text"] == "a\ufffdb"
    assert clean["pair"] == "\U0001f600"
    assert clean["('tuple', 1)"] == [1, 2]
    assert clean["\ufffd"] == "key had a lone surrogate"


@given(json_like)
def test_sanitized_values_never_panic_pycrdt(value):
    doc = pycrdt.Doc()
    array = doc.get("a", type=pycrdt.Array)
    # A pyo3 panic is a BaseException: it would escape any `except Exception`.
    array.append(docsafe.sanitize_for_doc(value))


def test_stream_keeps_the_tail_with_a_notice():
    text = "x" * (docsafe.STREAM_KEEP_CHARS + 10) + "END"
    capped = docsafe.cap_outputs_for_doc(
        [{"output_type": "stream", "name": "stdout", "text": text}]
    )
    assert capped[0]["text"].endswith("END")
    assert capped[0]["text"].startswith("[nh: 13 earlier characters")
    assert len(capped[0]["text"]) < docsafe.STREAM_KEEP_CHARS + 200


def test_big_bundle_becomes_a_notice_and_cell_total_is_capped():
    big = {
        "output_type": "display_data",
        "data": {"image/png": "A" * (docsafe.BUNDLE_MAX_CHARS + 1)},
        "metadata": {"image/png": {"width": 10}},
    }
    capped = docsafe.cap_outputs_for_doc([big])
    assert set(capped[0]["data"]) == {"text/plain"}
    assert "too large" in capped[0]["data"]["text/plain"]
    assert capped[0]["metadata"] == {}

    chunk = {"output_type": "display_data", "data": {"text/plain": "y" * (3 << 20)}, "metadata": {}}
    capped = docsafe.cap_outputs_for_doc([chunk, chunk, chunk, chunk])
    assert len(capped) == 3
    assert capped[-1]["output_type"] == "stream"
    assert "2 more output(s)" in capped[-1]["text"]


def test_inputs_are_not_mutated():
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": "x" * (docsafe.STREAM_KEEP_CHARS + 1)}
    ]
    docsafe.cap_outputs_for_doc(outputs)
    assert len(outputs[0]["text"]) == docsafe.STREAM_KEEP_CHARS + 1


def test_doc_outputs_strip_transient():
    outputs = [
        {
            "output_type": "display_data",
            "data": {"text/plain": "1"},
            "metadata": {},
            "transient": {"display_id": "abc"},
        }
    ]
    assert "transient" not in docsafe.doc_outputs(outputs)[0]
    assert "transient" in outputs[0]


def test_output_summary():
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": "\n  hello world\n"},
        {
            "output_type": "display_data",
            "data": {"image/png": "AAA", "text/plain": "<Figure>"},
            "metadata": {},
        },
        {"output_type": "error", "ename": "KeyError", "evalue": "'prce'", "traceback": []},
    ]
    summary = docsafe.output_summary(outputs)
    assert summary.count == 3
    assert summary.types == ["stream", "display_data", "error"]
    assert summary.error == "KeyError: 'prce'"
    assert summary.text_head == "hello world"
    assert summary.images == 1


def _stream(text, name="stdout"):
    return {"header": {"msg_type": "stream"}, "content": {"name": name, "text": text}}


def test_output_hook_merges_streams_and_collapses_progress_bars():
    outputs: list[dict] = []
    for text in ["a\n", "b\n", " 10%\r", " 20%\r", " 30%\n"]:
        output_hook(outputs, _stream(text))
    output_hook(outputs, _stream("warn\n", "stderr"))
    assert outputs == [
        {"output_type": "stream", "name": "stdout", "text": "a\nb\n 30%\n"},
        {"output_type": "stream", "name": "stderr", "text": "warn\n"},
    ]


@pytest.mark.parametrize("wait", [False, True])
def test_output_hook_clear_output(wait):
    outputs: list[dict] = []
    output_hook(outputs, _stream("old\n"))
    output_hook(outputs, {"header": {"msg_type": "clear_output"}, "content": {"wait": wait}})
    if wait:
        assert outputs[0]["text"] == "old\n"  # cleared only when the next output arrives
    output_hook(outputs, _stream("new\n"))
    assert outputs == [{"output_type": "stream", "name": "stdout", "text": "new\n"}]


def test_output_hook_update_display_data():
    outputs: list[dict] = []
    output_hook(
        outputs,
        {
            "header": {"msg_type": "display_data"},
            "content": {
                "data": {"text/plain": "1"},
                "metadata": {},
                "transient": {"display_id": "d"},
            },
        },
    )
    changed = output_hook(
        outputs,
        {
            "header": {"msg_type": "update_display_data"},
            "content": {
                "data": {"text/plain": "2"},
                "metadata": {},
                "transient": {"display_id": "d"},
            },
        },
    )
    assert changed
    assert outputs[0]["data"] == {"text/plain": "2"}


def test_stream_start_moves_only_past_the_slack():
    keep, slack = docsafe.STREAM_KEEP_CHARS, docsafe.STREAM_SLACK_CHARS
    assert docsafe.stream_start(10) == 0
    assert docsafe.stream_start(keep + 5) == 5  # a new stream keeps exactly the cap
    assert docsafe.stream_start(keep + slack, 0) == 0  # a running one grows at the end...
    assert docsafe.stream_start(keep + slack + 1, 0) == slack + 1  # ...until the slack is used up
    assert docsafe.stream_start(3, 50) == 0  # it shrank (\r, backspaces): start over
    capped = docsafe.cap_outputs_for_doc(
        [{"output_type": "stream", "name": "stdout", "text": "abcdef"}], starts=[2]
    )
    assert (
        capped[0]["text"]
        == "[nh: 2 earlier characters of this output were not saved in the notebook]\ncdef"
    )
