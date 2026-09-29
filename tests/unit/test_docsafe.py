"""exec/docsafe: values the CRDT document accepts, and output caps."""

from __future__ import annotations

import math

import pycrdt
import pytest
from hypothesis import given
from hypothesis import strategies as st

from nh_gateway._shared import secrets
from nh_gateway.backend.kernel import output_hook
from nh_gateway.exec import docsafe, shaping

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


def test_output_summary_is_redacted_before_it_is_cut(tmp_path):
    """The outline, turn summaries and nh_run(mode="wait") read these (design §6.8)."""
    before = docsafe.output_summary([{"output_type": "stream", "text": "token=abc123 ok"}])
    assert before.text_head == "token=[redacted:token] ok"  # patterns only, nothing installed
    password = "Sup3r" + "S3cret-Passw0rd-2026"  # fake
    (tmp_path / ".env").write_text(f"DB_PASSWORD={password}\n")
    secrets.install(secrets.Redactor.for_project(tmp_path, {}))
    evalue = "x" * (160 - len("RuntimeError: ") - 6) + password  # 6 chars fit before the cut
    head = "y" * 190 + password  # 10 chars fit before the 200-char cut
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": head + " and the rest\n"},
        {"output_type": "error", "ename": "RuntimeError", "evalue": evalue, "traceback": []},
    ]
    summary = docsafe.output_summary(outputs)
    assert (
        summary.error
        == ("RuntimeError: " + evalue.replace(password, "[redacted:DB_PASSWORD]"))[:160]
    )
    assert summary.text_head == (head.replace(password, "[redacted:DB_PASSWORD]"))[:200]
    assert password[:6] not in summary.error and password[:6] not in summary.text_head


GH = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0" + "A1s2D3f4G5h6J7k8"  # fake; a pattern-only secret
PLAIN = "Pq7Rs8Tu9Vw0" + "Xy1Za2Bc3De4"  # fake; a plain-named .env value: no fragment rule


@pytest.mark.parametrize("secret", [GH, PLAIN])
def test_a_stream_cut_never_starts_inside_a_secret(tmp_path, secret):
    """The cut moves to the next line: the tail of a cut secret is no pattern's any more, yet the
    cell view and the outline read this copy (design §6.8, review of C3)."""
    (tmp_path / ".env").write_text(f"SERVICE_DSN={PLAIN}\n")
    secrets.install(secrets.Redactor.for_project(tmp_path, {}))
    keep, line = docsafe.STREAM_KEEP_CHARS, "request ok\n"
    tail = secret[4:] + "\n"
    tail += line * ((keep - len(tail)) // len(line))
    tail += "z" * (keep - len(tail))
    text = "earlier output " + secret[:4] + tail  # the last 1 MiB starts 4 chars into it
    assert len(text) - keep == len("earlier output ") + 4
    [doc] = docsafe.doc_outputs([{"output_type": "stream", "name": "stdout", "text": text}])
    skipped = len("earlier output ") + len(secret) + 1
    notice = f"[nh: {skipped:,} earlier characters of this output were not saved in the notebook]"
    assert doc["text"] == notice + "\n" + text[skipped:]
    view = shaping.shape_outputs(
        [doc], max_chars=4000, max_images=0, image_max_px=512, save_dir=None
    ).text
    head = docsafe.output_summary([doc]).text_head
    assert head == doc["text"][:200]
    for shown in (doc["text"], view, head):
        assert secret[4:12] not in shown and secret[-8:] not in shown


def test_snap_cut_moves_to_a_line_then_a_word_then_a_value():
    assert docsafe.snap_cut("abc\ndef ghi", 1) == 4  # the next line
    assert docsafe.snap_cut("abcdef ghi", 1) == 7  # else the next word
    assert docsafe.snap_cut("abc,def", 1) == 4  # else the next value
    assert docsafe.snap_cut("abcdefghi", 1) == 1  # else where it was
    assert docsafe.snap_cut("abc\ndef", 4) == 4  # already at a line start
    assert docsafe.snap_cut("abc", 0) == 0 and docsafe.snap_cut("abc", 3) == 3
    far = "x" * docsafe.SNAP_MAX_CHARS + "\nrest"
    assert docsafe.snap_cut("a" + far, 1) == 1  # never further than SNAP_MAX_CHARS


def test_a_snapped_cut_still_only_appends_while_the_stream_grows():
    keep = docsafe.STREAM_KEEP_CHARS
    text = "log line\n" * (keep // 9 + 100) + "partial"
    start = docsafe.stream_start(len(text))
    first = docsafe.doc_outputs([{"output_type": "stream", "text": text}], [start])[0]["text"]
    assert first.startswith("[nh: ") and "\npartial" in first
    for more in (" line\n", "log line\n" * 50):
        text += more
        assert docsafe.stream_start(len(text), start) == start
        again = docsafe.doc_outputs([{"output_type": "stream", "text": text}], [start])[0]["text"]
        assert again.startswith(first)
        first = again


def test_a_long_traceback_line_is_cut_at_a_word():
    big = docsafe.BUNDLE_MAX_CHARS  # only an error over the bundle cap has its lines cut
    line = "x" * big + " " + GH + " " + "y" * 3963
    assert len(line) - 4000 == big + 1 + 4  # the kept 4000 chars would start 4 chars into GH
    [doc] = docsafe.doc_outputs(
        [{"output_type": "error", "ename": "E", "evalue": "v", "traceback": [line]}]
    )
    assert doc["traceback"] == ["y" * 3963]


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
