"""Output shaping: text budget, errors, images, mime choice and the full-copy files."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import random
import re
import string
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from nh_gateway._shared import secrets
from nh_gateway._shared.text import clip, strip_ansi, terminal_text
from nh_gateway.exec import shaping
from nh_gateway.exec.shaping import (
    MAX_HTML_CHARS,
    PNG_LIMIT,
    TRIM_CHARS,
    _cut,
    _cut_marker,
    _html_to_text,
    prune_outputs_dir,
    redact,
    shape_outputs,
    summarize_outputs,
)

MARKER = "chars cut …]"
CUT = re.compile(r"\n\[… ([\d,]+) chars cut …\]\n")


def shape(outputs, *, max_chars=2000, max_images=2, image_max_px=768, save_dir=None):
    return shape_outputs(
        outputs,
        max_chars=max_chars,
        max_images=max_images,
        image_max_px=image_max_px,
        save_dir=save_dir,
    )


def stream(text, name="stdout"):
    return {"output_type": "stream", "name": name, "text": text}


def bundle(data, otype="display_data"):
    out = {"output_type": otype, "data": data, "metadata": {}}
    if otype == "execute_result":
        out["execution_count"] = 1
    return out


def error(ename, evalue, traceback):
    return {"output_type": "error", "ename": ename, "evalue": evalue, "traceback": traceback}


def image_b64(width, height, *, mode="RGB", fmt="PNG", noise=False, seed=0):
    if noise:
        rng = random.Random(seed)
        bands = len(mode)
        img = Image.frombytes(mode, (width, height), rng.randbytes(width * height * bands))
    else:
        img = Image.new(mode, (width, height), "white" if mode != "P" else 0)
    buffer = io.BytesIO()
    img.save(buffer, fmt)
    return base64.b64encode(buffer.getvalue()).decode()


def outputs_dir(tmp_path: Path) -> Path:
    return tmp_path / "proj" / ".nh" / "outputs"


# A real ipykernel KeyError, trimmed: ANSI colours, a dashed rule, the user's frame, a library frame.
IPY_KEYERROR = [
    "\x1b[31m---------------------------------------------------------------------------\x1b[39m",
    "\x1b[31mKeyError\x1b[39m                                  Traceback (most recent call last)",
    "\x1b[36mCell\x1b[39m\x1b[36m \x1b[39m\x1b[32mIn[7]\x1b[39m\x1b[32m, line 3\x1b[39m\n"
    "\x1b[32m      1\x1b[39m \x1b[38;5;28;01mimport\x1b[39;00m \x1b[38;5;21;01mpandas\x1b[39;00m\n"
    "\x1b[32m      2\x1b[39m df = load()\n"
    "\x1b[32m----> \x1b[39m\x1b[32m3\x1b[39m df[\x1b[33m'prce'\x1b[39m]\n",
    "\x1b[36mFile \x1b[39m\x1b[32m~/proj/.venv/lib/python3.12/site-packages/pandas/core/frame.py:4102\x1b[39m, "
    "in \x1b[36mDataFrame.__getitem__\x1b[39m\x1b[34m(self, key)\x1b[39m\n"
    "\x1b[32m-> \x1b[39m\x1b[32m4102\x1b[39m indexer = self.columns.get_loc(key)\n",
    "\x1b[31mKeyError\x1b[39m: 'prce'",
]


# --- text -----------------------------------------------------------------------------------


def test_strip_ansi_removes_colours_hyperlinks_and_escapes():
    text = "\x1b[1;31mred\x1b[0m \x1b]8;;http://x\x07link\x1b]8;;\x07 \x1b[2Kdone\x1bM"
    assert strip_ansi(text) == "red link done"


def test_streams_are_coalesced_and_progress_bars_keep_the_last_state():
    shaped = shape(
        [
            stream("\r 10%"),
            stream("\r 50%"),
            stream("\r100%\n"),
            stream("warn\n", "stderr"),
            stream("done\r\n"),
        ]
    )
    assert shaped.text == "[stdout] 100%\n[stderr] warn\n[stdout] done"
    assert not shaped.truncated


def test_trailing_carriage_return_keeps_the_line():
    assert shape([stream("loading…\r")]).text == "[stdout] loading…"


def test_nbformat_multiline_lists_are_joined():
    shaped = shape([bundle({"text/plain": ["   price\n", "0      1"]}, "execute_result")])
    assert shaped.text == "[out]\n   price\n0      1"


def test_surrogates_and_control_characters_are_cleaned():
    shaped = shape([stream("a\ud800b\x00c\x07d\te")])
    assert shaped.text == "[stdout] a\ufffdbcd\te"


def test_short_output_is_untouched_and_nothing_is_saved(tmp_path):
    shaped = shape([stream("Dropped 312 rows\n")], save_dir=outputs_dir(tmp_path))
    assert shaped.text == "[stdout] Dropped 312 rows"
    assert shaped.full_path is None
    assert not outputs_dir(tmp_path).exists()


def test_no_outputs_give_empty_text():
    shaped = shape([])
    assert (shaped.text, shaped.images, shaped.error, shaped.truncated) == ("", [], None, False)


def test_long_stream_keeps_head_and_tail_and_saves_a_full_copy(tmp_path):
    lines = "".join(f"line {i}\n" for i in range(2000))
    shaped = shape([stream(lines)], save_dir=outputs_dir(tmp_path))

    assert len(shaped.text) <= 2000
    assert shaped.truncated
    assert shaped.text.startswith("[stdout]\nline 0\n")
    assert "line 1999\n[full output: .nh/outputs/" in shaped.text
    assert MARKER in shaped.text
    head, tail = shaped.text.split(MARKER)
    assert len(head) < len(tail)  # 30% head, 70% tail

    assert shaped.full_path is not None
    assert shaped.full_path.startswith(".nh/outputs/") and shaped.full_path.endswith(".txt")
    saved = (tmp_path / "proj" / shaped.full_path).read_text()
    assert saved == "[stdout]\n" + lines.rstrip("\n")
    assert Path(shaped.full_path).stem == hashlib.sha256(saved.encode()).hexdigest()[:16]


def test_truncation_without_save_dir_has_no_footer():
    shaped = shape([stream("x" * 5000)])
    assert shaped.truncated and shaped.full_path is None
    assert "full output" not in shaped.text
    assert len(shaped.text) <= 2000


def test_same_output_twice_reuses_the_saved_copy(tmp_path):
    first = shape([stream("y" * 5000)], save_dir=outputs_dir(tmp_path))
    second = shape([stream("y" * 5000)], save_dir=outputs_dir(tmp_path))
    assert first.full_path == second.full_path
    assert len(list(outputs_dir(tmp_path).iterdir())) == 1


def test_a_disk_failure_only_loses_the_copy(tmp_path):
    blocker = tmp_path / "proj" / ".nh"
    blocker.parent.mkdir(parents=True)
    blocker.write_text("not a directory")
    shaped = shape([stream("z" * 5000)], save_dir=blocker / "outputs")
    assert shaped.full_path is None
    assert shaped.truncated and len(shaped.text) <= 2000


def test_many_outputs_leave_out_the_middle():
    outputs = [bundle({"text/plain": f"table {i}\n" + "row\n" * 100}) for i in range(50)]
    shaped = shape(outputs)
    assert len(shaped.text) <= 2000
    assert "more outputs not shown" in shaped.text
    assert "table 0" in shaped.text and "table 49" in shaped.text
    assert "table 25" not in shaped.text


def test_short_outputs_stay_whole_when_a_long_one_is_cut():
    shaped = shape(
        [stream("x" * 10_000), bundle({"text/plain": "shape: (10432, 8)"}, "execute_result")]
    )
    assert "[out] shape: (10432, 8)" in shaped.text
    assert len(shaped.text) <= 2000


# --- errors ---------------------------------------------------------------------------------


def test_error_info_from_an_ipython_traceback():
    shaped = shape([error("KeyError", "'prce'", IPY_KEYERROR)])

    assert shaped.error is not None
    assert (shaped.error.ename, shaped.error.evalue, shaped.error.line) == ("KeyError", "'prce'", 3)
    assert "\x1b" not in shaped.error.traceback
    assert "Cell In[7], line 3" in shaped.error.traceback
    assert shaped.text.startswith("[error]\n")
    assert "-----" * 5 not in shaped.text  # the dashed rule is dropped
    assert "File …/site-packages/pandas/core/frame.py:4102" in shaped.text
    assert shaped.text.endswith("KeyError: 'prce'")
    assert shaped.summary.error == "KeyError: 'prce'"


def test_error_line_comes_from_the_last_exception_in_a_chain():
    traceback = [
        "Cell In[5], line 2\n----> 2 value = table['x']\n",
        "KeyError: 'x'",
        "\nThe above exception was the direct cause of the following exception:\n",
        "Cell In[5], line 4\n----> 4 raise ValueError('no x') from err\n",
        "ValueError: no x",
    ]
    assert shape([error("ValueError", "no x", traceback)]).error.line == 4


@pytest.mark.parametrize(
    ("evalue", "traceback", "line"),
    [
        (
            "invalid syntax (3212.py, line 4)",
            ["  File <unknown>\n", "SyntaxError: invalid syntax"],
            4,
        ),
        (
            "boom",
            ['Traceback (most recent call last):\n  File "<string>", line 6, in <module>\n'],
            6,
        ),
        ("boom", ["File /x/lib.py:12, in f()\n", "RuntimeError: boom"], None),
        ("", [], None),
    ],
)
def test_error_line_fallbacks(evalue, traceback, line):
    assert shape([error("SyntaxError", evalue, traceback)]).error.line == line


def test_error_without_traceback_shows_name_and_value():
    shaped = shape([error("ZeroDivisionError", "division by zero", [])])
    assert shaped.text == "[error] ZeroDivisionError: division by zero"


# pandas.to_datetime's multi-line message, as ipykernel sends it (the last frame is the error).
TO_DATETIME = (
    "day is out of range for month. You might want to try:\n"
    "    - passing `format` if your strings have a consistent format;\n"
    "    - passing `format='ISO8601'` if your strings are all ISO8601 but not necessarily in "
    "exactly the same format;\n"
    "    - passing `format='mixed'`, and the format will be inferred for each element "
    "individually. You might want to use `dayfirst` alongside this."
)


@pytest.mark.parametrize("gap", ["", "\n"])
def test_multi_line_error_message_appears_once(gap):
    # Review finding 51: the old check looked only at the traceback's last line.
    evalue = TO_DATETIME.replace(";\n", ";\n" + gap, 1)
    traceback = [
        *IPY_KEYERROR[:3],
        f"\x1b[31mValueError\x1b[39m: {evalue}",
    ]
    shaped = shape([error("ValueError", evalue, traceback)])
    assert shaped.text.count("day is out of range for month") == 1
    assert shaped.text.count("passing `format='mixed'`") == 1
    assert shaped.text.endswith("You might want to use `dayfirst` alongside this.")
    assert shaped.summary.error == (
        "ValueError: day is out of range for month. You might want to try…"
    )


def test_error_is_appended_once_when_the_traceback_lacks_it():
    traceback = IPY_KEYERROR[:3]
    shaped = shape([error("ValueError", TO_DATETIME, traceback)])
    assert shaped.text.count("day is out of range for month") == 1
    assert shaped.text.endswith("You might want to use `dayfirst` alongside this.")


def test_long_traceback_keeps_the_users_frame_and_the_tail():
    library = [
        f"File ~/.venv/lib/python3.12/site-packages/lib/mod{i}.py:{i}, in f{i}()\n"
        + "".join(f"   {n} some library code line number {n}\n" for n in range(8))
        for i in range(40)
    ]
    traceback = [*IPY_KEYERROR[:3], *library, IPY_KEYERROR[-1]]
    shaped = shape([error("KeyError", "'prce'", traceback)])

    assert len(shaped.text) <= 2000
    assert shaped.text.startswith("[error]\nCell In[7], line 3\n")
    assert "----> 3 df['prce']" in shaped.text
    assert shaped.text.count("Cell In[7]") == 1
    assert MARKER in shaped.text
    assert shaped.text.endswith("KeyError: 'prce'")


def test_error_gets_most_of_the_budget():
    library = [f"File /site-packages/lib/m{i}.py:1, in f()\n" + "   code\n" * 10 for i in range(60)]
    shaped = shape(
        [
            stream("noise\n" * 3000),
            error("KeyError", "'prce'", [*IPY_KEYERROR[:3], *library, IPY_KEYERROR[-1]]),
        ]
    )
    stdout, err = shaped.text.split("\n[error]\n")
    assert len(shaped.text) <= 2000
    assert len(err) >= 1300
    assert len(stdout) >= 200


# --- mime choice ----------------------------------------------------------------------------


def test_dataframe_prefers_text_plain():
    data = {"text/plain": "   price\n0      1", "text/html": "<table><tr><td>1</td></tr></table>"}
    assert shape([bundle(data, "execute_result")]).text == "[out]\n   price\n0      1"


def test_html_only_output_becomes_tab_separated_text():
    html = (
        "<div><style scoped>.dataframe th {text-align: right;}</style>"
        "<table><thead><tr><th></th><th>price</th><th>region</th></tr></thead>"
        "<tbody><tr><th>0</th><td>1.5</td><td>North &amp; East</td></tr>"
        "<tr><th>1</th><td>2</td><td>South</td></tr></tbody></table>"
        "<p>2 rows<br>2 columns</p><script>alert(1)</script></div>"
    )
    text = shape([bundle({"text/html": html})]).text
    assert (
        text == "[display]\n\tprice\tregion\n0\t1.5\tNorth & East\n1\t2\tSouth\n2 rows\n2 columns"
    )


def test_object_repr_gives_way_to_richer_mime_types():
    outputs = [
        bundle({"text/plain": "<IPython.core.display.HTML object>", "text/html": "<b>hi</b>"}),
        bundle(
            {"text/plain": "<IPython.core.display.Markdown object>", "text/markdown": "**bold**"}
        ),
        bundle({"text/plain": "<IPython.core.display.JSON object>", "application/json": {"a": 1}}),
    ]
    assert shape(outputs).text == '[display] hi\n[display] **bold**\n[display] {"a":1}'


def test_json_with_nan_and_big_ints_is_compact_text():
    value = {"x": float("nan"), "big": 2**80, "list": [1, 2]}
    text = shape([bundle({"application/json": value})]).text
    assert text == '[display] {"x":NaN,"big":1208925819614629174706176,"list":[1,2]}'


@pytest.mark.parametrize(
    ("data", "text"),
    [
        (
            {
                "application/vnd.plotly.v1+json": {"data": [{}, {}], "layout": {}},
                "text/html": "<div/>",
            },
            "[display] [plotly figure: 2 traces]",
        ),
        ({"application/vnd.plotly.v1+json": {"data": [{}]}}, "[display] [plotly figure: 1 trace]"),
        (
            {
                "application/vnd.jupyter.widget-view+json": {"model_id": "a"},
                "text/plain": "IntSlider(value=0)",
            },
            "[display] [widget]",
        ),
        ({"image/svg+xml": "<svg/>"}, "[display] [SVG figure omitted]"),
        ({"text/latex": "$x^2$"}, "[display] $x^2$"),
        ({"application/x-custom": "…"}, "[display] [application/x-custom output omitted]"),
        ({"text/plain": "<Axes: >"}, "[display] <Axes: >"),
    ],
)
def test_placeholders_and_fallbacks(data, text):
    assert shape([bundle(data)]).text == text


def test_unknown_output_type_is_named():
    assert shape([{"output_type": "weird"}, "not a dict"]).text == "[weird output]"


# --- images ---------------------------------------------------------------------------------


def test_small_png_is_sent_unchanged_and_not_saved(tmp_path):
    data = image_b64(640, 480)
    shaped = shape(
        [bundle({"image/png": data, "text/plain": "<Figure size 640x480 with 1 Axes>"})],
        save_dir=outputs_dir(tmp_path),
    )

    (image,) = shaped.images
    assert image.data == base64.b64decode(data)
    assert (image.mime, image.width, image.height, image.orig) == (
        "image/png",
        640,
        480,
        (640, 480),
    )
    assert shaped.text == "[image 1: 640x480 png]"  # the figure repr adds nothing
    assert shaped.full_path is None and not outputs_dir(tmp_path).exists()


def test_large_image_is_downsampled_and_the_original_saved(tmp_path):
    data = image_b64(2000, 1000)
    shaped = shape([bundle({"image/png": data})], save_dir=outputs_dir(tmp_path))

    (image,) = shaped.images
    assert (image.mime, image.width, image.height, image.orig) == (
        "image/png",
        768,
        384,
        (2000, 1000),
    )
    assert Image.open(io.BytesIO(image.data)).size == (768, 384)
    raw = base64.b64decode(data)
    original = outputs_dir(tmp_path) / f"{hashlib.sha256(raw).hexdigest()[:16]}.png"
    assert original.read_bytes() == raw
    assert shaped.text.startswith("[image 1: 2000x1000 png, sent at 768x384]\n[full output: ")
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    assert f"original: .nh/outputs/{original.name}" in full


def test_png_over_150kb_is_sent_as_jpeg():
    shaped = shape([bundle({"image/png": image_b64(700, 700, noise=True)})])
    (image,) = shaped.images
    assert image.mime == "image/jpeg"
    assert image.data[:2] == b"\xff\xd8"
    assert (image.width, image.height) == (700, 700)
    assert len(base64.b64decode(image_b64(700, 700, noise=True))) > PNG_LIMIT


def test_rgba_and_palette_images_survive_re_encoding():
    rgba = shape([bundle({"image/png": image_b64(900, 900, mode="RGBA", noise=True)})]).images[0]
    assert rgba.mime == "image/jpeg" and Image.open(io.BytesIO(rgba.data)).mode == "RGB"
    palette = shape([bundle({"image/png": image_b64(1200, 300, mode="P")})]).images[0]
    assert (palette.mime, palette.width, palette.height) == ("image/png", 768, 192)


def test_large_jpeg_stays_jpeg():
    shaped = shape([bundle({"image/jpeg": image_b64(1600, 1200, fmt="JPEG", noise=True)})])
    (image,) = shaped.images
    assert (image.mime, image.width, image.height) == ("image/jpeg", 768, 576)
    assert shaped.text == "[image 1: 1600x1200 jpeg, sent at 768x576]"


def test_images_beyond_the_limit_are_dropped_but_kept_on_disk(tmp_path):
    outputs = [bundle({"image/png": image_b64(100 + i, 100)}) for i in range(3)]
    shaped = shape(outputs, max_images=2, save_dir=outputs_dir(tmp_path))

    assert [i.width for i in shaped.images] == [100, 101]
    assert shaped.dropped_images == 1
    assert "[image 3: 102x100 png, not sent (limit 2)]" in shaped.text
    assert len([p for p in outputs_dir(tmp_path).iterdir() if p.suffix == ".png"]) == 1


def test_unreadable_image_is_a_placeholder_and_does_not_use_the_limit():
    outputs = [
        bundle({"image/png": "not base64 at all!"}),
        bundle({"image/png": base64.b64encode(b"GIF89a garbage").decode()}),
        bundle({"image/png": image_b64(50, 50)}),
    ]
    shaped = shape(outputs, max_images=1)
    assert shaped.text.splitlines()[:2] == [
        "[image 1: could not be read]",
        "[image 2: could not be read]",
    ]
    assert [i.width for i in shaped.images] == [50]
    assert shaped.dropped_images == 2


# --- summary, redaction, pruning ------------------------------------------------------------


def test_summarize_outputs_without_decoding_images():
    outputs = [
        stream("\x1b[32mloaded\x1b[0m 10,432 rows\n"),
        bundle({"image/png": "not-even-base64", "text/plain": "<Figure size 640x480 with 1 Axes>"}),
        bundle({"text/plain": "<IPython.core.display.HTML object>", "text/html": "<b>x</b>"}),
        error("KeyError", "\x1b[31m'prce'\x1b[0m", []),
    ]
    summary = summarize_outputs(outputs)
    assert summary.count == 4
    assert summary.types == ["stdout", "image/png", "text/html", "error"]
    assert summary.error == "KeyError: 'prce'"
    assert summary.text_head == "loaded 10,432 rows"
    assert summary.images == 1


def test_summary_heads_are_cut_at_a_word_boundary():
    printed = "rows kept after the price filter " * 10
    summary = summarize_outputs([stream(printed), error("ValueError", "x " * 100, [])])
    head = summary.text_head
    assert head is not None and len(head) <= 80 and head.endswith("…")
    assert printed.startswith(head[:-1] + " ")  # whole words only
    assert summary.error is not None and len(summary.error) <= 120
    assert summary.error.endswith(" x…")


def test_shaped_carries_the_summary():
    assert shape([stream("hi\n")]).summary.types == ["stdout"]


PASSWORD = "Sup3r" + "S3cret-Passw0rd-2026"  # fake; under DB_PASSWORD in the project's .env


def install_secret(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(f"DB_PASSWORD={PASSWORD}\n")
    secrets.install(secrets.Redactor.for_project(root, {}))


def test_redact_is_the_installed_redactor(tmp_path):
    """v0.1's identity seam is FR-14's redactor (design §6.8), patterns-only until installed."""
    assert redact("token=abc123 SECRET") == "token=[redacted:token] SECRET"
    install_secret(tmp_path)
    assert (
        redact(f"pw {PASSWORD} token=abc123") == "pw [redacted:DB_PASSWORD] token=[redacted:token]"
    )


def test_every_text_output_and_the_full_copy_are_redacted(tmp_path):
    install_secret(tmp_path)
    frame = f"\x1b[36m----> \x1b[39m3 connect('postgresql://app:{PASSWORD}@db')"
    outputs = [
        stream(f"connecting with {PASSWORD}\n" + "row\n" * 3000),
        stream(f"warning: {PASSWORD}\n", name="stderr"),
        bundle({"text/plain": f"'{PASSWORD}'"}, "execute_result"),
        bundle({"text/markdown": f"**{PASSWORD}**"}),
        bundle({"text/latex": f"$${PASSWORD}$$"}),
        bundle({"application/json": {"password": PASSWORD}}),
        error("RuntimeError", f"login failed for {PASSWORD}", [frame, f"RuntimeError: {PASSWORD}"]),
    ]
    shaped = shape(outputs, save_dir=outputs_dir(tmp_path))
    assert shaped.truncated and shaped.full_path is not None
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    assert shaped.error is not None and shaped.summary.error is not None
    texts = [shaped.text, full, shaped.error.evalue, shaped.error.traceback, shaped.summary.error]
    for text in texts + [shaped.summary.text_head or ""]:
        assert PASSWORD[:8] not in text, text
    assert full.count("[redacted:DB_PASSWORD]") == 8  # 2 streams, 4 bundles, the error's 2 lines
    assert "connecting with [redacted:DB_PASSWORD]" in shaped.text
    assert "connect('postgresql://[redacted:DB_PASSWORD]@db')" in shaped.error.traceback
    assert shaped.summary.error == "RuntimeError: login failed for [redacted:DB_PASSWORD]"
    assert Path(shaped.full_path).stem == hashlib.sha256(full.encode()).hexdigest()[:16]


def test_images_are_never_scanned(tmp_path, monkeypatch):
    install_secret(tmp_path)
    redactor = secrets.current()
    scanned: list[str] = []
    real = redactor.redact

    def spy(text):  # redact_head and the module's redact() both end up here
        scanned.append(text)
        return real(text)

    monkeypatch.setattr(redactor, "redact", spy)
    data = image_b64(640, 480)
    shaped = shape([bundle({"image/png": data}), stream(f"saved with {PASSWORD}\n")])
    assert shaped.images[0].data == base64.b64decode(data)
    assert shaped.text == "[image 1: 640x480 png]\n[stdout] saved with [redacted:DB_PASSWORD]"
    assert scanned and not any(data[:64] in text for text in scanned if isinstance(text, str))
    assert max(len(text) for text in scanned) < 200


def test_html_over_1_mb_is_redacted_before_its_cut(tmp_path):
    install_secret(tmp_path)
    head = "<table><tr>" + "<td>x</td>" * ((MAX_HTML_CHARS - 40) // 10) + "<td>"
    head += "y" * (MAX_HTML_CHARS - 10 - len(head))  # the password starts 10 chars before the cut
    markup = f"{head}{PASSWORD}</td></tr></table>"
    assert markup.index(PASSWORD) == MAX_HTML_CHARS - 10
    text = _html_to_text(markup)
    assert PASSWORD[:10] not in text and text.endswith("[… HTML cut …]")
    assert "[redacted:" in text
    shaped = shape([bundle({"text/html": markup})])
    assert PASSWORD[:10] not in shaped.text


def test_summary_heads_are_redacted_before_they_are_cut(tmp_path):
    install_secret(tmp_path)
    blank = "\n" * 3990  # the first line starts 10 chars before the 4000-char window's end
    summary = summarize_outputs([stream(blank + PASSWORD + " rest\n")])
    assert summary.text_head == "[redacted:DB_PASSWORD] rest"
    summary = summarize_outputs([error("RuntimeError", f"{'x ' * 50}{PASSWORD}", [])])
    assert summary.error is not None and PASSWORD[:4] not in summary.error


@pytest.mark.parametrize("inside", [4, 11])  # under FRAGMENT_MIN: no cut-piece rule catches it
@pytest.mark.parametrize("pad", ["\n", " \x01\n", "\x1b[0m\n"])  # blank as a terminal shows it
def test_the_first_line_is_never_cut_inside_a_secret(tmp_path, inside, pad):
    """The window once began at the text's start: with only ``inside`` chars of the secret in
    it, their head became text_head (review of C3)."""
    install_secret(tmp_path)
    margin = secrets.current().margin
    blank = (pad * (4000 + margin))[: 4000 + margin - inside]
    summary = summarize_outputs([stream(blank + PASSWORD + " rest\n")])
    assert summary.text_head == "[redacted:DB_PASSWORD] rest"


def test_the_first_line_window_grows_until_the_line_ends(tmp_path):
    install_secret(tmp_path)
    margin = secrets.current().margin
    line = "word " * ((4000 + margin) // 5 - 2) + PASSWORD  # the secret sits across the edge
    summary = summarize_outputs([stream(line + "\nnext\n")])
    assert summary.text_head is not None and PASSWORD[:4] not in summary.text_head
    assert summary.text_head.startswith("word word") and len(summary.text_head) <= 80
    long_line = "x" * 5_000_000  # a line with no end: the window stops growing at 1 MiB
    assert summarize_outputs([stream(long_line)]).text_head == clip("x" * 81, 80)


def test_prune_keeps_the_newest_files(tmp_path):
    for i in range(5):
        path = tmp_path / f"f{i}.txt"
        path.write_bytes(b"x" * 10)
        os.utime(path, (1000 + i, 1000 + i))
    (tmp_path / ".f9.txt.tmp").write_bytes(b"in flight")

    prune_outputs_dir(tmp_path, max_files=3)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        ".f9.txt.tmp",
        "f2.txt",
        "f3.txt",
        "f4.txt",
    ]

    prune_outputs_dir(tmp_path, max_files=10, max_bytes=25)
    assert sorted(p.name for p in tmp_path.iterdir()) == [".f9.txt.tmp", "f3.txt", "f4.txt"]


def test_prune_of_a_missing_dir_is_a_no_op(tmp_path):
    prune_outputs_dir(tmp_path / "missing")


def test_prune_never_deletes_the_files_it_is_told_to_keep(tmp_path):
    for i in range(5):
        path = tmp_path / f"f{i}.txt"
        path.write_bytes(b"x" * 10)
        os.utime(path, (1000 + i, 1000 + i))
    (tmp_path / "big.txt").write_bytes(b"x" * 100)
    os.utime(tmp_path / "big.txt", (999, 999))  # the oldest, and alone over the byte cap

    prune_outputs_dir(tmp_path, max_files=3, keep={"f0.txt"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["f0.txt", "f3.txt", "f4.txt"]

    (tmp_path / "big.txt").write_bytes(b"x" * 100)
    os.utime(tmp_path / "big.txt", (999, 999))
    prune_outputs_dir(tmp_path, max_bytes=25, keep={"big.txt", "f3.txt"})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["big.txt", "f3.txt"]


def test_a_call_s_own_copies_survive_its_prune(tmp_path, monkeypatch):
    """a7 (design §6.13): one output list with a long stream and images over the byte cap. The
    full copy and every original it names are still there afterwards (once, the originals, older
    than the full copy by a few ms, were pruned the moment they were written)."""
    import nh_gateway.exec.shaping as shaping

    real_prune = shaping.prune_outputs_dir
    monkeypatch.setattr(
        shaping, "prune_outputs_dir", lambda path, **kw: real_prune(path, max_bytes=20_000, **kw)
    )
    save = outputs_dir(tmp_path)
    save.mkdir(parents=True)
    old = save / "0123456789abcdef.txt"
    old.write_bytes(b"x" * 100)
    os.utime(old, (1000, 1000))
    images = [bundle({"image/png": image_b64(300, 300, noise=True, seed=s)}) for s in range(3)]
    shaped = shape([stream("line\n" * 3000), *images], save_dir=save)
    assert shaped.full_path
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    originals = [part.split("]")[0] for part in full.split("; original: ")[1:]]
    assert len(originals) == 3
    for name in [shaped.full_path, *originals]:
        assert (tmp_path / "proj" / name).is_file(), name
    assert not old.exists()  # an older call's copy makes room


# --- trim (design §6.8) ---------------------------------------------------------------------

SMALL_TRIM = 200_000  # a TRIM_CHARS the tests fill fast; the margin stays the real one
ANSI_LINES = "\x1b[0m" * 199 + "ok1\n"  # 800 raw chars, 4 shown: cleaning shrinks it


def trim_lengths() -> tuple[int, int, int]:
    """The head and tail a text over SMALL_TRIM keeps, and the margin."""
    margin = shaping._trim_margin()
    keep = SMALL_TRIM - 2 * margin
    return int(keep * shaping.HEAD_SHARE), keep - int(keep * shaping.HEAD_SHARE), margin


TRIM_CUT = re.compile(r"(?:\n|^)\[… ([\d,]+) chars cut …\](?:\n|$)")  # a side may be empty


def split_copy(full: str, label: str = "[stdout]\n") -> tuple[str, int, str]:
    """A trimmed text's head, the count in its one trim marker, and its tail."""
    assert full.startswith(label), full[:80]
    body = full[len(label) :]
    [match] = TRIM_CUT.finditer(body)
    return body[: match.start()], int(match.group(1).replace(",", "")), body[match.end() :]


def result_output(shaped) -> str:
    return shaped.text.rsplit("\n[full output: ", 1)[0]


def grams(text: str, size: int = 6) -> set[str]:
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def count_cleaning(monkeypatch) -> tuple[list[int], list[int]]:
    """The lengths of the texts shaping cleans (``terminal_text``) and redacts (``redact``,
    ``redact_at``) from now on, on the installed redactor."""
    redactor = secrets.current()
    cleaned: list[int] = []
    redacted: list[int] = []
    real_terminal, real_redact, real_at = shaping.terminal_text, redactor.redact, redactor.redact_at

    def terminal(text):
        cleaned.append(len(text))
        return real_terminal(text)

    def spy(text):
        redacted.append(len(text))
        return real_redact(text)

    def spy_at(text, at):
        redacted.append(len(text))
        return real_at(text, at)

    monkeypatch.setattr(shaping, "terminal_text", terminal)
    monkeypatch.setattr(redactor, "redact", spy)
    monkeypatch.setattr(redactor, "redact_at", spy_at)
    return cleaned, redacted


def test_a_50_mb_stream_is_cleaned_only_up_to_the_cap(tmp_path, monkeypatch):
    install_secret(tmp_path)
    redactor = secrets.current()
    cleaned, redacted = count_cleaning(monkeypatch)
    line = "epoch 12/100 - loss: 0.2345 - acc: 0.9123 - see https://example.com/x?a=1\n"
    raw = line * (50_000_000 // len(line)) + f"done {PASSWORD}\n"
    shaped = shape([stream(raw)], save_dir=outputs_dir(tmp_path))

    first_line = shaping.FIRST_LINE_WINDOW + redactor.margin  # the summary's text_head window
    assert sum(cleaned) <= TRIM_CHARS + first_line, sum(cleaned)
    assert sum(redacted) <= TRIM_CHARS + first_line, sum(redacted)
    assert shaped.truncated and shaped.full_path is not None
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    assert len(full) <= TRIM_CHARS and PASSWORD not in full
    head, count, tail = split_copy(full)
    assert raw.startswith(head + "\n") and tail.endswith("\ndone [redacted:DB_PASSWORD]")
    raw_tail = tail.replace("[redacted:DB_PASSWORD]", PASSWORD)
    assert raw.endswith("\n" + raw_tail + "\n")
    assert count == len(raw) - 1 - len(head) - len(raw_tail)  # the raw chars not kept
    # The result counts the real total: what its own cut and the trim left out.
    output = result_output(shaped)
    assert len(shaped.text) <= 2000
    [match] = CUT.finditer(output)
    shown_head, shown_tail = output[len("[stdout]\n") : match.start()], output[match.end() :]
    shown = int(match.group(1).replace(",", ""))
    assert shown_tail.endswith("done [redacted:DB_PASSWORD]")
    raw_shown_tail = shown_tail.replace("[redacted:DB_PASSWORD]", PASSWORD)
    assert shown == len(raw) - 1 - len(shown_head) - len(raw_shown_tail)


def test_the_trim_keeps_whole_lines_of_the_head_and_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    raw = "".join(f"line {i}\n" for i in range(300_000))
    shaped = shape([stream(raw)], save_dir=outputs_dir(tmp_path))
    head_len, tail_len, _ = trim_lengths()

    full = (tmp_path / "proj" / shaped.full_path).read_text()
    head, count, tail = split_copy(full)
    assert raw.startswith(head + "\n") and raw.endswith("\n" + tail + "\n")
    assert head_len - 20 <= len(head) <= head_len and tail_len - 20 <= len(tail) <= tail_len
    assert count == len(raw) - 1 - len(head) - len(tail)
    assert shaped.truncated


def test_the_cap_is_where_the_trim_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    for size, cut in ((SMALL_TRIM, False), (SMALL_TRIM + 1, True)):
        raw = ("row\n" * size)[:size]
        shaped = shape([stream(raw)], save_dir=outputs_dir(tmp_path))
        full = (tmp_path / "proj" / shaped.full_path).read_text()
        assert (CUT.search(full) is not None) is cut
        if not cut:
            assert full == "[stdout]\n" + raw.rstrip("\n")


def test_a_text_within_the_real_cap_is_kept_whole(tmp_path):
    line = "row 12345 ok\n"
    raw = line * (TRIM_CHARS // len(line))
    shaped = shape([stream(raw)], save_dir=outputs_dir(tmp_path))
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    assert full == "[stdout]\n" + raw.rstrip("\n")
    assert Path(shaped.full_path).stem == hashlib.sha256(full.encode()).hexdigest()[:16]


def pem_block(rng: random.Random, lines: int) -> tuple[str, list[str]]:
    body = [fake(rng, 64, string.ascii_letters + string.digits + "+/") for _ in range(lines)]
    key = "\n".join(["-----BEGIN RSA PRIVATE KEY-----", *body, "-----END RSA PRIVATE KEY-----"])
    return key, body


def fake(rng: random.Random, size: int, alphabet: str = string.ascii_letters + string.digits):
    return "".join(rng.choice(alphabet) for _ in range(size))


def edge_secret(kind: str) -> tuple[str, list[str]]:
    """A secret to plant, and the parts of it no output may show any 6 chars of."""
    rng = random.Random(kind)
    if kind == ".env value":
        return PASSWORD, [PASSWORD]
    if kind == "github token":
        token = "ghp_" + fake(rng, 36)
        return token, [token]
    if kind == "private key":
        return pem_block(rng, 26)
    if kind == "private key over 8 KB":  # an unterminated key's run was once cut at 8 KiB
        return pem_block(rng, 190)
    if kind == "private key over 16 KiB":  # its END line is past PEM_MAX_CHARS: a key run
        return pem_block(rng, 300)
    password = fake(rng, 20)
    return f"postgresql://app:{password}@db.example.com/prod", [password]


EDGE_KINDS = [
    ".env value",
    "github token",
    "private key",
    "private key over 8 KB",
    "private key over 16 KiB",
    "userinfo",
]
EDGES = ["head cut", "head window end", "tail cut", "tail window start"]


def edge_at(edge: str, size: int) -> int:
    """Where ``edge`` is in a text of ``size`` raw chars trimmed to SMALL_TRIM."""
    head_len, tail_len, margin = trim_lengths()
    return {
        "head cut": head_len,
        "head window end": head_len + margin,
        "tail cut": size - tail_len,
        "tail window start": size - tail_len - margin,
    }[edge]


@pytest.mark.parametrize("edge", EDGES)
@pytest.mark.parametrize("kind", EDGE_KINDS)
def test_a_secret_across_a_trim_edge_is_redacted_whole(tmp_path, monkeypatch, kind, edge):
    """The secret over each edge at several offsets. The head's edges are in one long line
    (nothing to snap to); the tail's in lines of 60,000 chars, as the tail keeps whole lines."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    secret, parts = edge_secret(kind)
    size = 2 * SMALL_TRIM
    at = edge_at(edge, size)
    fill = ". " * SMALL_TRIM if edge.startswith("head") else ("word " * 12_000 + "\n") * 7
    secret_grams = set().union(*(grams(part) for part in parts))
    for before in (1, len(secret) // 2, len(secret) - 1):  # chars of it before the edge
        start = at - before
        raw = (fill[:start] + secret + fill[start + len(secret) :])[:size]
        save = outputs_dir(tmp_path) / f"{before}"
        shaped = shape([stream(raw)], max_chars=SMALL_TRIM, save_dir=save)
        full = (save / Path(shaped.full_path).name).read_text()
        head, _, tail = split_copy(full)  # trimmed, with one marker
        assert head and (tail or edge.startswith("head"))
        assert "[full output: " in shaped.text and CUT.search(shaped.text)  # shown whole
        for where, text in (("result", shaped.text), ("full copy", full)):
            leaked = secret_grams & grams(text)
            assert not leaked, (kind, edge, before, where, sorted(leaked)[:3])


def test_texts_the_cap_can_t_share_are_left_out_from_the_middle(tmp_path, monkeypatch):
    monkeypatch.setattr(shaping, "TRIM_CHARS", 1_000_000)
    monkeypatch.setattr(shaping, "TRIM_MIN_SHARE", 150_000)
    streams = [
        stream((f"out {i:02d}\n" + "x\n" * 75_000)[:150_000], "stdout" if i % 2 else "stderr")
        for i in range(30)
    ]
    outputs = [
        *streams[:10],
        error("ValueError", "boom", ["ValueError: boom"]),
        *streams[10:15],
        bundle({"image/png": image_b64(20, 20)}),
        *streams[15:],
    ]
    shaped = shape(outputs, save_dir=outputs_dir(tmp_path))
    full = (tmp_path / "proj" / shaped.full_path).read_text()

    # The first three and the last three stay whole, and the error; each run between is a line.
    for i in (0, 1, 2, 27, 28, 29):
        assert f"out {i:02d}\n" in full
    for i in range(3, 27):
        assert f"out {i:02d}\n" not in full
    runs = [int(n.replace(",", "")) for n in re.findall(r"(?m)^\[… ([\d,]+) chars cut …\]$", full)]
    assert runs == [7 * 150_000, 5 * 150_000, 12 * 150_000]
    assert "[error] ValueError: boom" in full and "[image 1: 20x20 png]" in full
    assert full.index("[error]") < full.index("[image 1") < full.index("out 27")
    assert len(shaped.text) <= 2000
    # A result that leaves out a run counts every output the run stands for.
    text = shape(outputs, max_chars=600).text
    [more] = re.findall(r"\[… (\d+) more outputs not shown …\]", text)
    shown = sum(f"out {i:02d}\n" in text for i in range(30)) + ("[image 1" in text)
    shown += sum(count for chars, count in RUNS.items() if f"[… {chars} chars cut …]" in text)
    assert int(more) == 31 - shown and int(more) > 10, text


RUNS = {"1,050,000": 7, "750,000": 5, "1,800,000": 12}  # the runs above: their chars, outputs


def test_a_long_error_is_trimmed_and_keeps_its_line(tmp_path, monkeypatch):
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    evalue = "bad value: " + "v" * 1_000_000 + f" {PASSWORD}"
    frames = ["Cell In[3], line 2\n----> 2 check(x)\n", f"ValueError: {evalue}"]
    shaped = shape([error("ValueError", evalue, frames)], save_dir=outputs_dir(tmp_path))

    assert shaped.error is not None and shaped.error.line == 2
    # one long line each: its head is kept, then the trim marker (no tail: §6.8)
    assert TRIM_CUT.search(shaped.error.evalue) and TRIM_CUT.search(shaped.error.traceback)
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    assert len(full) < SMALL_TRIM + 1000
    for text in (full, shaped.text, shaped.error.evalue, shaped.summary.error or ""):
        assert PASSWORD[:8] not in text
    assert shaped.summary.error.startswith("ValueError: bad value: vvv")
    assert len(shaped.text) <= 2000
    counts = [int(n.replace(",", "")) for n in CUT.findall(shaped.text)]
    assert counts and max(counts) > 1_000_000  # the result counts what the trims left out


def test_the_error_summary_cleans_a_long_value_within_its_share(tmp_path, monkeypatch):
    install_secret(tmp_path)
    redactor = secrets.current()
    scanned: list[int] = []
    real = redactor.redact

    def spy(text):
        scanned.append(len(text))
        return real(text)

    monkeypatch.setattr(redactor, "redact", spy)
    summary = summarize_outputs([error("ValueError", "bad " + "v" * 5_000_000, [])])
    assert sum(scanned) <= shaping.TRIM_MIN_SHARE + 4096  # and error_summary's own head window
    assert summary.error is not None and summary.error.startswith("ValueError: bad vvv")


def test_a_cut_never_shows_part_of_a_trim_marker():
    marker = "[… 5,000,000 chars cut …]"
    text = "a\n" + marker + "\n" + "b" * 300
    out = _cut(text, 80, [(marker, 5_000_000)])  # the head would end inside the marker
    [match] = CUT.finditer(out)
    head, tail = out[: match.start()], out[match.end() :]
    assert head == "a\n" and text.endswith(tail) and "chars cut" not in head + tail
    left_out = len(text) - len(head) - len(tail) - len(marker)  # the marker stands for its count
    assert int(match.group(1).replace(",", "")) == left_out + 5_000_000


@pytest.mark.parametrize("inside", [1, 5, 12, 24])  # chars of the password before the cut
def test_a_trim_cut_never_splits_a_redaction_marker(tmp_path, inside):
    """The head's cut falls inside the password: ``redact_at`` maps it to its marker's start,
    so the head leaves the marker out whole."""
    install_secret(tmp_path)
    margin = shaping._trim_margin()
    window = "x " * 50 + PASSWORD + " " + "y " * margin  # one line: no break to snap to
    redacted = len(window) - len(PASSWORD) + len("[redacted:DB_PASSWORD]")
    assert shaping._trim_head(window, 100 + inside, margin) == ("x " * 50, redacted - 100)
    assert shaping._trim_head(window, 100, margin)[0] == "x " * 50
    head, left = shaping._trim_head(window, 100 + len(PASSWORD) + 1, margin)
    assert head == "x " * 50 + "[redacted:DB_PASSWORD] " and left == redacted - len(head)


def test_the_head_snaps_to_a_line_break_only_in_its_last_40_percent(tmp_path):
    install_secret(tmp_path)
    margin = shaping._trim_margin()
    for newline, kept in ((700, 700), (500, 1000)):  # of a 1,000-char head
        window = "a " * (newline // 2) + "\n" + "b " * (margin + 1000)
        assert len(shaping._trim_head(window, 1000, margin)[0]) == kept


def wbr(secret: str, every: int = 8) -> str:
    """``secret`` as HTML splits it, joined again only when parsed."""
    return "<wbr>".join(secret[i : i + every] for i in range(0, len(secret), every))


@pytest.mark.parametrize("edge", ["markup cut", "text cut"])
def test_html_over_its_share_shows_no_piece_of_a_secret_the_parser_joins(
    tmp_path, monkeypatch, edge
):
    """R2: the markup is cut at the share before it is parsed, inside a secret only the
    parsed text holds whole; the head is cut in the parsed, redacted text."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    margin = shaping._trim_margin()
    token = "ghp_" + fake(random.Random(edge), 36)
    for secret in (PASSWORD, token):
        split = wbr(secret)
        at = SMALL_TRIM if edge == "markup cut" else SMALL_TRIM - margin  # where its edge is
        for before in (1, len(split) // 2, len(split) - 1):  # chars of it before the edge
            pad = at - before - len("<p></p><p>")
            markup = f"<p>{'x ' * (pad // 2)}{'x' * (pad % 2)}</p><p>{split}</p>"
            markup += "<p>more</p>" * ((2 * SMALL_TRIM - len(markup)) // 11)
            shaped = shape([bundle({"text/html": markup})], save_dir=outputs_dir(tmp_path))
            full = (tmp_path / "proj" / shaped.full_path).read_text()
            for text in (full, shaped.text):
                leaked = grams(secret) & grams(text)
                assert not leaked, (secret[:4], before, sorted(leaked)[:3])
            assert full.endswith("[… HTML cut …]") and full.startswith("[display]\nx x x")


def test_html_over_the_cap_is_parsed_only_within_its_share(tmp_path, monkeypatch):
    """TD-3: three HTML outputs over the cap: only their shares of markup are parsed, cleaned
    and redacted, and each says it was cut."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", 600_000)
    monkeypatch.setattr(shaping, "TRIM_MIN_SHARE", 100_000)
    fed: list[int] = []
    real_feed = shaping._HtmlText.feed

    def feed(self, data):
        fed.append(len(data))
        return real_feed(self, data)

    monkeypatch.setattr(shaping._HtmlText, "feed", feed)
    cleaned, redacted = count_cleaning(monkeypatch)
    rows = "".join(f"<tr><td>{i}</td><td>{PASSWORD}</td></tr>" for i in range(9_000))
    outputs = [bundle({"text/html": f"<table>{rows}</table>"}) for _ in range(3)]
    assert len(rows) > 400_000
    shaped = shape(outputs, save_dir=outputs_dir(tmp_path))

    # each its 200,000-char share, redacted as markup first (its markers are shorter)
    assert len(fed) == 3 and all(180_000 < chars <= 200_000 for chars in fed), fed
    first_line = shaping.FIRST_LINE_WINDOW + secrets.current().margin  # text_head's window
    assert sum(cleaned) <= 600_000 + first_line
    margin = shaping._trim_margin()  # the markup is redacted a margin past each share
    assert sum(redacted) <= 3 * (200_000 + margin) + 600_000 + first_line
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    assert full.count("[… HTML cut …]") == 3 and PASSWORD[:6] not in full
    assert len(re.findall(r"(?m)^0\t\[redacted:DB_PASSWORD\]$", full)) == 3  # each from its start


def test_the_trim_s_cost_does_not_grow_with_the_output(tmp_path, monkeypatch):
    """Counted, not timed (TD-6): 8 times the output cleans and redacts the same chars."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", 1_000_000)
    line = f"epoch 1 - loss 0.25 - token=abc123def456 - db {PASSWORD}\n"

    def counted(size: int) -> tuple[int, int]:
        with pytest.MonkeyPatch.context() as patch:
            cleaned, redacted = count_cleaning(patch)
            text = line * (size // len(line))
            shape([stream(text), error("ValueError", text, [text]), bundle({"text/html": text})])
        return sum(cleaned), sum(redacted)

    small, large = counted(2_000_000), counted(16_000_000)
    assert large[0] <= small[0] + 1000 and large[1] <= small[1] + 1000, (small, large)
    summary = shaping.TRIM_MIN_SHARE + shaping.FIRST_LINE_WINDOW + secrets.current().margin + 10
    assert large[0] <= 1_000_000 + summary  # the error summary (name, value) and text_head


def trim_copy(tmp_path: Path, raw: str, name: str = "copy") -> tuple[Any, str]:
    """``raw`` as one stream through shape_outputs (the result at 2000 chars) and its copy."""
    save = outputs_dir(tmp_path) / name
    shaped = shape([stream(raw)], save_dir=save)
    return shaped, (save / Path(shaped.full_path).name).read_text()


def shown_grams(secret: str, *texts: str) -> set[str]:
    return set().union(*(grams(secret) & grams(text) for text in texts))


@pytest.mark.parametrize("lead", [5_000, 100, 1])  # its literal's chars before the window
@pytest.mark.parametrize(
    "literal", ["Authorization: Bearer ", '{"access_token": "', 'password = "', "token="]
)
def test_a_secret_longer_than_the_margin_never_shows_past_the_tail_start(
    tmp_path, monkeypatch, literal, lead
):
    """R1: the tail window starts inside a 40,000-char value whose literal is left of it; the
    whole text redacts the value, so no 6 chars of it may show."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    value = fake(random.Random(literal), 40_000)
    secret = literal + value + ('"' if literal.endswith('"') else "")
    fills = {"one line": ". " * SMALL_TRIM, "lines": "loss 0.1 ok\n" * (2 * SMALL_TRIM // 12)}
    for name, fill in fills.items():
        start = edge_at("tail window start", len(fill)) - lead
        raw = fill[:start] + secret + fill[start + len(secret) :]
        assert not shown_grams(value, shaping._clean(raw)), name  # the whole text redacts it
        shaped, full = trim_copy(tmp_path, raw, name)
        assert not shown_grams(value, full, shaped.text), (name, literal, lead)
        if name == "lines":
            assert full.endswith("loss 0.1 ok")  # the tail: the lines after the value's


@pytest.mark.parametrize("kind", ["github token", "userinfo"])
@pytest.mark.parametrize("edge", ["head window end", "tail window start"])
@pytest.mark.parametrize(
    "fill",
    [ANSI_LINES, "\x1b[0m", "token=abc12345\n"],
    ids=["shrinks", "shrinks to one line", "grows"],  # when cleaned
)
def test_a_secret_across_a_shrunk_or_grown_window_s_edge_is_redacted_whole(
    tmp_path, monkeypatch, kind, edge, fill
):
    """TD-2: cleaning shrinks a window by almost all of it (terminal codes, in lines or in one
    line) or grows it (markers longer than the values): the cuts are still a margin inside the
    cleaned window, or at a line start, and the secret across a raw edge shows no 6 chars."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    secret, parts = edge_secret(kind)
    secret += " " * (-len(secret) % 4)  # replace whole 4-char escape codes
    size = 2 * SMALL_TRIM
    at = edge_at(edge, size)
    base = (fill * (size // len(fill) + 1))[:size]
    for before in (1, len(secret) // 2, len(secret) - 1):
        start = (at - before) // 4 * 4
        raw = base[:start] + secret + base[start + len(secret) :]
        shaped, full = trim_copy(tmp_path, raw, f"{edge}-{before}")
        for part in parts:
            assert not shown_grams(part, full, shaped.text), (kind, edge, before)


def test_long_texts_over_the_cap_each_keep_a_head_and_tail(tmp_path, monkeypatch):
    """TD-4: three texts, each over a third of the cap, split it (``_water_fill``): each keeps
    its head and tail; none is left out."""
    monkeypatch.setattr(shaping, "TRIM_CHARS", 1_000_000)
    names = ["first", "second", "last"]
    outputs = [
        stream("".join(f"{name} {i}\n" for i in range(50_000)), "stderr" if i == 1 else "stdout")
        for i, name in enumerate(names)
    ]
    shaped = shape(outputs, save_dir=outputs_dir(tmp_path))
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    sections = re.split(r"(?m)^\[(?:stdout|stderr)\]\n", full)[1:]
    assert len(sections) == 3, full[:200]
    for name, body in zip(names, sections, strict=True):
        head, count, tail = split_copy("[x]\n" + body.rstrip("\n"), "[x]\n")
        assert head.startswith(f"{name} 0\n") and tail.endswith(f"{name} 49999")
        assert 200_000 < len(head) + len(tail) < 1_000_000 // 3
        assert count == len(outputs[names.index(name)]["text"]) - 1 - len(head) - len(tail)


@pytest.mark.parametrize("limit", range(60, 117, 7))
def test_a_cut_never_shows_part_of_a_trim_marker_at_either_end(limit):
    """TD-5: the result's tail may start inside a trim marker too."""
    marker = "[… 5,000,000 chars cut …]"
    text = "a" * 300 + "\n" + marker + "\n" + "b" * 10
    out = _cut(text, limit, [(marker, 5_000_000)])
    rest = re.sub(r"\[… [\d,]+ chars cut …\]", "", out)
    assert "…" not in rest and "chars" not in rest and "cut" not in rest, out


def test_two_trim_markers_alike_are_each_found():
    marker = _cut_marker(5_000)
    text = "a" + marker + "b" + marker + "c"
    first, second = 1, 2 + len(marker)
    assert shaping._trim_spans(text, [(marker, 5_000), (marker, 5_000)]) == [
        (first, first + len(marker), 5_000),
        (second, second + len(marker), 5_000),
    ]


def test_an_error_s_frames_are_each_cleaned_alone_over_the_cap(tmp_path, monkeypatch):
    """R3: a frame's end is a text's end to the redactor (a cut piece of a value there is
    redacted), even when the error is over its share."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    piece = PASSWORD[:16]
    frames = [
        "Cell In[1], line 3\n----> 3 connect(pw)\n",
        f"File ~/lib/db.py:9, in connect(pw)\n      9 raise ValueError(pw[:16]) -> {piece}",
        "ValueError: bad password",
    ]
    for evalue in ("bad password", "bad password " + "v" * 1_000_000):
        out = [error("ValueError", evalue, frames)]
        shaped = shape(out, save_dir=outputs_dir(tmp_path))
        full = (tmp_path / "proj" / shaped.full_path).read_text() if shaped.full_path else ""
        assert shaped.error is not None
        for text in (shaped.text, full, shaped.error.traceback):
            assert piece not in text
        assert "-> [redacted:DB_PASSWORD]" in shaped.error.traceback


MULTI_ENV = 'MULTI_SECRET="Line1-Abc123xyzKq\\nLine2-Def456uvwZr"\n'  # a value with a line break


@pytest.mark.parametrize("dotenv", ["", MULTI_ENV])
def test_a_progress_bar_over_the_cap_keeps_its_last_state(tmp_path, monkeypatch, dotenv):
    """R4: ``\\r`` rewrites shrink the windows to a line or two when cleaned; the tail still
    keeps the final state, as the whole text shows it."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / ".env").write_text(dotenv)
    secrets.install(secrets.Redactor.for_project(root, {}))
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    steps = "".join(f"\rstep {i:6d}/30000 loss {1 / (i + 1):.6f}" for i in range(30_000))
    final = "step  29999/30000 loss 0.000033"
    for raw, last in (
        (steps + "\nfinal accuracy 0.9731\n", f"{final}\nfinal accuracy 0.9731"),
        (steps, final),
        ("".join(f"Epoch {e}/40\n{steps[:30_000]} - val {e}\n" for e in range(40)), "- val 39"),
    ):
        shaped, full = trim_copy(tmp_path, raw)
        assert full.endswith(last) and result_output(shaped).endswith(last), full[-200:]
        assert len(shaped.text) <= 2000
    assert full.count("Epoch") >= 6  # the keras-like one: whole epochs in its head and tail


def test_a_value_with_a_line_break_across_the_tail_start_is_left_out(tmp_path, monkeypatch):
    """A ``.env`` value with a line break whose first line ends where the tail would start:
    its next line is skipped (``Redactor.spill``); a line that only starts like it is too."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / ".env").write_text(MULTI_ENV)
    secrets.install(secrets.Redactor.for_project(root, {}))
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    _, _, margin = trim_lengths()
    rows = "row 1 ok\n" * (2 * SMALL_TRIM // 9)
    window = edge_at("tail window start", len(rows))
    for second in ("Line2-Def456uvwZr rest", "Line2-other rest"):
        end = f" Line1-Abc123xyzKq\n{second}\n"
        pad = margin + 2000 - (margin + 2000 + len(end)) % 9
        planted = "x" * pad + end  # whole rows; its first line ends past the margin
        start = (window - 1000) // 9 * 9
        raw = rows[:start] + planted + rows[start + len(planted) :]
        assert "Def456" not in shaping._clean(raw)  # the whole text redacts the value
        shaped, full = trim_copy(tmp_path, raw, second[:8])
        _, _, tail = split_copy(full)
        assert "Line2" not in tail and tail.startswith("row 1 ok")


@pytest.mark.parametrize("inside", [1, 9, 16])  # chars of its second line in the window
def test_a_value_with_a_line_break_across_the_head_window_end_is_left_out(
    tmp_path, monkeypatch, inside
):
    """The head window's raw end cuts a ``.env`` value's second line, in a window cleaning
    shrinks to a few lines: the head ends the value's length before the window's last line,
    so its first line, which only the whole value marks, is not kept."""
    root = tmp_path / "proj"
    root.mkdir(parents=True)
    (root / ".env").write_text(MULTI_ENV)
    secrets.install(secrets.Redactor.for_project(root, {}))
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    fill = ANSI_LINES * (2 * SMALL_TRIM // len(ANSI_LINES))
    end = edge_at("head window end", len(fill))
    planted = "Line1-Abc123xyzKq\nLine2-Def456uvwZr\n"
    start = end - inside - len("Line1-Abc123xyzKq\n")
    line_start = fill.rfind("\n", 0, start) + 1
    raw = fill[:line_start] + "\x1b[0m" * ((start - line_start) // 4) + fill[line_start:]
    raw = raw[:start] + planted + raw[start + len(planted) :]
    assert raw.index("Line2") + inside == end and "Line1" not in shaping._clean(raw)
    shaped, full = trim_copy(tmp_path, raw)
    assert "Line1" not in full and "Abc123" not in full and "ok1" in split_copy(full)[0]


@pytest.mark.parametrize("key", ["encrypted", "indented"])
def test_a_private_key_across_the_head_window_end_is_left_out(tmp_path, monkeypatch, key):
    """A key whose BEGIN line is in the head window and whose END line is past its raw end,
    in a window that cleaning shrinks before the key, so a line break inside the key is where
    the head would end. The window's key has no END line, and its key-shaped run stops at a
    header line (or an indent), where the whole text's runs on to the END line: the head ends
    a margin before the window's last line, before the BEGIN line."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    head_len, _, margin = trim_lengths()
    rng = random.Random(key)
    block, body = pem_block(rng, 120)
    if key == "encrypted":
        begin, rest = block.split("\n", 1)
        block = f"{begin}\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,{fake(rng, 32)}\n\n{rest}"
    else:
        block = "\n".join("    " + line for line in block.split("\n"))
    shrink = ANSI_LINES * ((margin - 2000) // 796)  # 796 fewer chars a line once cleaned
    rows = "row 1 ok\n" * ((head_len - 2000 - len(shrink) // 200) // 9)
    raw = rows + shrink + block + "\n"
    raw += "row 2 ok\n" * ((2 * SMALL_TRIM - len(raw)) // 9)
    window_end = edge_at("head window end", len(raw))
    assert len(rows + shrink) < window_end < len(rows + shrink + block)  # the key crosses it
    whole = shaping._clean(raw)
    assert not any(line in whole for line in body)  # the whole text redacts it to its END
    shaped, full = trim_copy(tmp_path, raw)
    for text in (full, shaped.text):
        assert not any(line[:12] in text for line in body), key
    assert split_copy(full)[0].startswith("row 1 ok\n")


@pytest.mark.parametrize("key", ["encrypted", "unterminated", "over 16 KiB"])
def test_a_private_key_across_the_tail_start_is_left_out(tmp_path, monkeypatch, key):
    """A key whose BEGIN line is before the tail window, in a window that cleaning shrinks so
    the tail would start right after the window's first line: its END line is looked for and
    the lines up to it skipped (a header line ends the first line: no key body there), or,
    with no END line, its key-shaped run is."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    rng = random.Random(key)
    lines = 300 if key == "over 16 KiB" else 40
    block, body = pem_block(rng, lines)
    if key == "encrypted":
        begin, rest = block.split("\n", 1)
        block = f"{begin}\nProc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,{fake(rng, 32)}\n\n{rest}"
    if key == "unterminated":
        block = block.rsplit("\n", 1)[0]
    fill = ANSI_LINES * (2 * SMALL_TRIM // len(ANSI_LINES))
    at = edge_at("tail window start", len(fill))
    lead = block.index("DEK-Info") + 10 if key == "encrypted" else 2000  # its chars before it
    start = (at - lead) // 4 * 4
    planted = "\n" + block + "\n"
    raw = fill[:start] + planted + fill[start + len(planted) :]
    whole = shaping._clean(raw)
    assert not any(line in whole for line in body)
    shaped, full = trim_copy(tmp_path, raw)
    for text in (full, shaped.text):
        assert not any(line[:12] in text for line in body), key
    assert full.endswith("ok1")


def test_lines_that_look_like_a_key_s_body_are_kept_without_a_begin_line(tmp_path, monkeypatch):
    """Lines of key-shaped chars only (ids, hashes, numbers) keep their tail: only a BEGIN
    line before the tail can open a key there."""
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    raw = "".join(f"{i}\n" for i in range(100_000))
    shaped, full = trim_copy(tmp_path, raw)
    head, _, tail = split_copy(full)
    assert head.startswith("0\n1\n") and tail.endswith("\n99999") and len(tail) > 100_000


def test_the_head_backs_off_a_keyed_run_that_runs_past_its_margin(tmp_path, monkeypatch):
    """A run a pattern judges whole (``sk-`` needs a digit, here its last char) crosses the
    head's cut and runs past what is redacted with it: the head ends before the run."""
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    head_len, _, margin = trim_lengths()
    letters = fake(random.Random(9), margin + 5_000, string.ascii_letters)
    raw = ". " * (head_len // 2 - 50) + f"key sk-{letters}9 " + ". " * SMALL_TRIM
    assert letters[:20] not in shaping._clean(raw)  # the whole text redacts it
    shaped, full = trim_copy(tmp_path, raw)
    head, _, _ = split_copy(full)
    assert head.endswith(". key") and not shown_grams(letters, full, shaped.text)


def env_project(tmp_path: Path, dotenv: str) -> None:
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(dotenv)
    secrets.install(secrets.Redactor.for_project(root, {}))


@pytest.mark.parametrize(
    "first", ["<table><tr><td>{}</td></tr>", "<p>{}</p><table>", "<div>x {} y</div><table>"]
)
def test_html_over_1_mb_over_its_share_redacts_its_markup_first(tmp_path, monkeypatch, first):
    """P1 (C12 review): the parser joins a ``.env`` value's line breaks (or collapses its two
    spaces) into one space, so only the markup holds it whole: trimmed HTML over 1 MB redacts
    its markup before it parses it, as it is redacted whole (``redact_head``)."""
    multi, spaced = "Kx7q2Lm9Pz4Wt8Rb\nNv3Hs6Jd1Yc5Fg0Qa", "spGFD78b6roA  sqKnP1hSjAKZ"
    env_project(
        tmp_path,
        f'SERVICE_PASSWORD="Kx7q2Lm9Pz4Wt8Rb\\nNv3Hs6Jd1Yc5Fg0Qa"\nSPACED_SECRET="{spaced}"\n',
    )
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    for value in (multi, spaced):
        start = first.format(value)
        markup = start + "<tr><td>row</td><td>0.5</td></tr>" * ((1_200_000 - len(start)) // 33)
        markup += "</table>"
        assert len(markup) > shaping.MAX_HTML_CHARS
        assert "[redacted:" in shaping._clean(shaping._html_to_text(markup))[:60], value
        shaped = shape([bundle({"text/html": markup})], save_dir=outputs_dir(tmp_path))
        full = (tmp_path / "proj" / shaped.full_path).read_text()
        assert full.endswith("[… HTML cut …]") and "row\t0.5" in full
        for text in (full, shaped.text):
            for part in value.split():
                assert part[:8] not in text, (value, text[:80])


@pytest.mark.parametrize("split", ["{}<wbr>{}", "{}<b>{}</b>", "{}&#97;{}"])
def test_html_of_1_mb_or_less_over_its_share_redacts_its_text_alone(tmp_path, monkeypatch, split):
    """HTML of 1 MB or less is redacted as parsed text alone when whole, which judges a token
    split by ``<wbr>``, a tag or an entity whole: the trim doesn't redact its markup first,
    which would mark the token's first part and show the rest (review of C12)."""
    env_project(tmp_path, "")
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    token = "ghp_" + fake(random.Random(3), 36)
    first, rest = token[:26], token[26:]
    start = "<p>key " + split.format(first, rest) + " done</p>"
    markup = start + "<p>row 0.5</p>" * ((900_000 - len(start)) // 14)
    assert len(markup) <= shaping.MAX_HTML_CHARS
    assert "key [redacted:github-token] done" in shaping._clean(shaping._html_to_text(markup))
    shaped = shape([bundle({"text/html": markup})], save_dir=outputs_dir(tmp_path))
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    for text in (full, shaped.text):
        assert "key [redacted:github-token] done" in text and rest[:6] not in text, text[:80]


@pytest.mark.parametrize("opener", ["<!-->", "<!--->", "<!--"])
def test_html_cut_inside_a_comment_shows_none_of_it(tmp_path, monkeypatch, opener):
    """P3b (C12 review): a comment open at the markup's cut. html.parser closes ``<!-->`` at once
    when no ``-->`` follows in what it was given, so the cut markup showed the comment's text,
    where the whole markup's later ``-->`` hides it and joins a value split around it."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    intro = "<p>intro line</p>" * 2000
    markup = f"{intro}<p>{PASSWORD[:14]}{opener}" + "<p>note</p>" * 40_000
    markup += f"-->{PASSWORD[14:]}</p><p>end</p>"
    whole = shaping._clean(shaping._html_text(markup))
    assert "[redacted:DB_PASSWORD]" in whole and "note" not in whole
    shaped = shape([bundle({"text/html": markup})], save_dir=outputs_dir(tmp_path))
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    for text in (full, shaped.text):
        assert not grams(PASSWORD) & grams(text) and "note" not in text, text[-200:]
    assert full.startswith("[display]\nintro line\n") and full.endswith("[… HTML cut …]")


def test_html_cut_inside_a_long_tag_shows_none_of_it(tmp_path, monkeypatch):
    """A tag still open at the markup's cut, longer than a margin (an image's data URI): the
    whole markup's parse shows none of it, and neither does the trimmed head. The cut markup is
    never closed: some html.parser versions (CPython 3.12.3's) show what ``close()`` finds still
    open as text."""
    install_secret(tmp_path)
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    image = "iVBORw0KGgo" + "QUJDRA" * (SMALL_TRIM // 6)
    markup = "<p>intro line</p>" * 2000 + f'<img src="data:image/png;base64,{image}"><p>end</p>'
    assert "base64" not in shaping._clean(shaping._html_text(markup))

    def no_close(self):
        raise AssertionError("the cut markup was closed")

    monkeypatch.setattr(shaping._HtmlText, "close", no_close)
    shaped = shape([bundle({"text/html": markup})], save_dir=outputs_dir(tmp_path))
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    for text in (full, shaped.text):
        assert "base64" not in text and "QUJDRA" not in text and "<img" not in text, text[-200:]
    assert full.startswith("[display]\nintro line\n") and full.endswith("[… HTML cut …]")


def test_the_html_prefix_shows_only_what_the_whole_markup_shows():
    """``_html_prefix``: at every cut of markups that open a construct across it (comments,
    abrupt ones, declarations, CDATA, tags, quoted attributes, scripts, a title, entities), the
    lines before the text's last are the whole markup's own."""
    markups = [
        "<p>a</p><!-->b<p>c</p>--><p>d</p><!--->e<p>f</p>--!><p>g</p>",
        "<p>a<!-- x <p>y</p> -->b</p><![CDATA[c<p>d]]><p>e</p><?pi <p>x?><p>f</p>",
        '<p>a</p><a title="x>y<p>z">b</a><p>c</p><!DOCTYPE html><p>d</p>',
        "<p>a</p><script>var s = '<p>x</p>';</script><p>b</p><title>t<p>u</title><p>c</p>",
        "<table><tr><td>a</td><td>b&amp;c</td></tr><tr><td>&#65;&notit;</td></tr></table><p>d</p>",
        "<pre>a\nb\r\nc</pre><p>d<br>e</p><!-- <!-- --><p>f</p><!--><!--><p>g</p>-->h",
        "<p>a</p><!--->b<p>c</p><!--->d<p>e</p>",  # cut after the second: the first is open too
    ]
    for markup in markups:
        whole = terminal_text(shaping._html_text(markup))
        for cut in range(len(markup) + 1):
            part = terminal_text(shaping._html_prefix(markup[:cut]))
            kept = part[: part.rfind("\n") + 1].rstrip("\n")
            assert whole.startswith(kept), (markup[:cut], part, whole)


@pytest.mark.parametrize(
    "begin",
    [
        "\x1b[1m-----\x1b[0mBEGIN RSA PRIVATE KEY-----",
        "-----BEG\x1b[0mIN PRIVATE KEY-----",
        "-----BEGIN\x1b[0m PRIVATE KEY-----",
        "-----BEG\x00IN PRIVATE KEY-----",
    ],
    ids=["bold dashes", "colour inside", "colour before the space", "control char"],
)
@pytest.mark.parametrize("end", [True, False], ids=["END line", "unterminated"])
def test_a_key_whose_begin_line_only_forms_once_cleaned_is_left_out(
    tmp_path, monkeypatch, begin, end
):
    """P9 (C12 review): a terminal escape or control character inside ``-----BEGIN `` hides the
    BEGIN line from a search of the raw text; the whole text strips it and redacts the key. The
    tail window starts 40 lines into its body, which runs more than a margin past that."""
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    _, tail_len, margin = trim_lengths()
    body = [
        fake(random.Random(i), 64, string.ascii_letters + string.digits + "+/") for i in range(600)
    ]
    key = begin + "\n" + "\n".join(body) + ("\n-----END PRIVATE KEY-----" if end else "") + "\n"
    after = tail_len + margin - (len(key) - len(begin) - 1 - 40 * 65)
    raw = "log line\n" * 30_000 + key + ("after line\n" * (after // 11 + 1))[:after]
    whole = shaping._clean(raw)
    assert not any(line in whole for line in body)
    shaped, full = trim_copy(tmp_path, raw)
    for text in (full, shaped.text):
        assert not any(line[:12] in text for line in body)
    assert "after line" in split_copy(full)[2]  # the tail goes on past the key


@pytest.mark.parametrize(
    "begin",
    [
        "\x1b[1m-----\x1b[0mBEGIN RSA PRIVATE KEY-----",
        "-----BEG\x1b[0mIN PRIVATE KEY-----",
        "-----BEGIN\x1b[0m PRIVATE KEY-----",
    ],
    ids=["bold dashes", "colour inside", "colour before the space"],
)
def test_a_key_coloured_more_than_two_margins_before_the_tail_is_left_out(
    tmp_path, monkeypatch, begin
):
    """P9 (C12 review): a BEGIN line that a terminal escape splits, more than two margins
    before the tail window, out of the control-character check's reach: the escape before the
    window's first line ends is what makes the tail assume a BEGIN line (``_begin_before``).
    The unterminated key's body runs into the window; the tail starts after it."""
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    _, tail_len, margin = trim_lengths()
    body = [
        fake(random.Random(i), 64, string.ascii_letters + string.digits + "+/")
        for i in range(1_400)
    ]
    lead = (2 * margin + 5_000) // 65 + 1  # its body lines before the tail window
    key = begin + "\n" + "\n".join(body) + "\n"
    after = tail_len + margin - (len(key) - len(begin) - 1 - lead * 65)
    raw = "log line\n" * 30_000 + key + ("after line\n" * (after // 11 + 1))[:after]
    start = len(raw) - tail_len - margin  # the tail window's
    assert raw.rfind("\x1b") < start - 2 * margin  # past the control-character check
    whole = shaping._clean(raw)
    assert not any(line in whole for line in body)
    shaped, full = trim_copy(tmp_path, raw)
    for text in (full, shaped.text):
        assert not any(line[:12] in text for line in body)
    assert "after line" in split_copy(full)[2]  # the tail goes on past the key


def test_a_trim_count_is_never_negative(tmp_path, monkeypatch):
    """P2 (C12 review): markers longer than the values they replace grow the kept head and tail
    past the raw text; the trim marker still counts what it left out: the raw chars between its
    windows and what each window left out, at least the two margins."""
    env_project(tmp_path, "DB_PASSWORD=Abc12345x\n")
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    margin = shaping._trim_margin()
    raw = ("db password Abc12345x ok\n" * 10_000)[: SMALL_TRIM + 100]
    shaped, full = trim_copy(tmp_path, raw)
    head, count, tail = split_copy(full)
    assert len(head) + len(tail) > len(raw) and "Abc12345x" not in full
    assert 100 + 2 * margin <= count <= 100 + 4 * margin
    assert all(int(n.replace(",", "")) > 0 for n in CUT.findall(shaped.text))


def test_a_trimmed_error_keeps_its_message_and_line(tmp_path, monkeypatch):
    """P5 (C12 review): an error with a long value and eight long frames, given about
    TRIM_MIN_SHARE among large outputs: split evenly each part would get under two margins and
    keep nothing. The name, the value and the frames from both ends keep a head and tail; the
    frames between keep nothing."""
    monkeypatch.setattr(shaping, "TRIM_CHARS", 1_000_000)
    monkeypatch.setattr(shaping, "TRIM_MIN_SHARE", 150_000)
    evalue = "could not convert column 'price' to float: " + "x" * 120_000
    frame = "Cell In[3], line 2\n----> 2 df.astype(float)\n" + "  File lib.py, line 9 in f\n" * 4000
    frames = [frame.replace("lib.py", f"lib{i}.py") for i in range(8)]
    streams = [
        stream(("filler line\n" * 13_000)[:150_000], "stdout" if i % 2 else "stderr")
        for i in range(20)
    ]
    shaped = shape(
        [*streams[:10], error("ValueError", evalue, frames), *streams[10:]],
        save_dir=outputs_dir(tmp_path),
    )
    assert shaped.error is not None and shaped.error.line == 2
    assert shaped.error.evalue.startswith("could not convert column 'price' to float: xxx")
    full = (tmp_path / "proj" / shaped.full_path).read_text()
    section = full[full.index("[error]") :]
    assert "lib0.py" in section and "lib7.py" in section  # the user's cell, where it was raised
    assert "Cell In[3], line 2" in section and "could not convert column 'price'" in section


def test_a_part_a_trimmed_error_cuts_keeps_part_keep_chars_or_nothing(tmp_path, monkeypatch):
    """PART_KEEP (C12 review): each part of an error that the trim cuts gets at least two
    margins plus PART_KEEP of its share, so its head and tail keep about PART_KEEP chars, or it
    keeps only a trim marker. Here the value and five frames, split evenly, would each get two
    margins plus half of PART_KEEP: the value and four frames from both ends get more, the
    middle frame keeps nothing."""
    margin = shaping._trim_margin()
    share = len("ValueError") + 6 * (2 * margin + shaping.PART_KEEP // 2)
    monkeypatch.setattr(shaping, "TRIM_CHARS", share)
    evalue = "".join(f"row {j}: could not convert 'abc{j}' to float\n" for j in range(1_500))
    frames = [
        "".join(f"  File lib{i}.py, line {j}, in step_{j}\n" for j in range(1_800))
        for i in range(5)
    ]
    shaped = shape([error("ValueError", evalue, frames)], save_dir=outputs_dir(tmp_path))
    assert shaped.error is not None and TRIM_CUT.search(shaped.error.evalue)

    def kept(text: str, tag: str) -> int:
        return sum(len(line) + 1 for line in text.split("\n") if tag in line)

    value = kept(shaped.error.evalue, "could not convert")
    each = [kept(shaped.error.traceback, f"lib{i}.py") for i in range(5)]
    assert value >= shaping.PART_KEEP, value
    assert all(chars == 0 or chars >= shaping.PART_KEEP for chars in each), each
    assert each[0] and each[-1] and each[2] == 0, each  # both ends kept, the middle not


def test_the_summary_cleans_the_error_whole_within_the_cap(tmp_path, monkeypatch):
    """TD-8 (C12 review): within the cap the summary's error value is cleaned whole, as before
    the trim (a ``\\r`` past 256 KiB rewrites its line); past the cap it is cleaned within
    TRIM_MIN_SHARE."""
    install_secret(tmp_path)
    evalue = "x" * 300_000 + "\rfinal message " + PASSWORD
    within = shape([error("ValueError", evalue, [])]).summary.error
    assert within == "ValueError: final message [redacted:DB_PASSWORD]"
    monkeypatch.setattr(shaping, "TRIM_CHARS", SMALL_TRIM)
    over = shape([error("ValueError", evalue, [])]).summary.error or ""
    assert over.startswith("ValueError: xxx") and PASSWORD[:8] not in over


# --- the budget always holds ----------------------------------------------------------------

_texts = st.text(alphabet=st.characters(codec="utf-8"), max_size=3000)
_outputs = st.lists(
    st.one_of(
        st.builds(stream, _texts, st.sampled_from(["stdout", "stderr"])),
        st.builds(lambda t: bundle({"text/plain": t}, "execute_result"), _texts),
        st.builds(lambda t: bundle({"text/html": f"<p>{t}</p>"}), _texts),
        st.builds(lambda v, tb: error("ValueError", v, tb), _texts, st.lists(_texts, max_size=5)),
    ),
    max_size=30,
)


@settings(max_examples=150, deadline=None)
@given(outputs=_outputs, max_chars=st.integers(min_value=80, max_value=4000))
def test_text_never_exceeds_max_chars(outputs, max_chars):
    shaped = shape(outputs, max_chars=max_chars)
    assert len(shaped.text) <= max_chars
    if not shaped.truncated:
        assert MARKER not in shaped.text
