"""Output shaping: text budget, errors, images, mime choice and the full-copy files."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import random
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from nh_gateway._shared import secrets
from nh_gateway.exec.shaping import (
    MAX_HTML_CHARS,
    PNG_LIMIT,
    _html_to_text,
    prune_outputs_dir,
    redact,
    shape_outputs,
    strip_ansi,
    summarize_outputs,
)

MARKER = "chars cut …]"


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
