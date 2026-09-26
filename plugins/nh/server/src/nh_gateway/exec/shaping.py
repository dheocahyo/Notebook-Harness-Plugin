"""Turn a cell's nbformat outputs into what the agent reads: a text budget, a few images, the error.

Nothing here talks to the kernel or the notebook. When the text had to be cut or an image was
downsampled, the untruncated text and the original images go to ``.nh/outputs/`` so nothing is lost.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from PIL import Image

from .._shared.text import clip
from ..backend.base import ErrorInfo, OutputSummary
from ..render import error_summary

ERROR_SHARE = 0.7  # of the text budget kept for the traceback when other output competes
MIN_SHARE = 200  # characters every shown output gets before any output is left out
HEAD_SHARE = 0.3  # head+tail cuts keep 30% head, 70% tail
PNG_LIMIT = 150 * 1024  # larger PNGs are re-encoded as JPEG q80
MAX_DECODE_PIXELS = 40_000_000
MAX_HTML_CHARS = 1_000_000

_ANSI = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI: colours, cursor movement
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"  # OSC: titles, hyperlinks
    r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"  # DCS, SOS, PM, APC
    r"|\x1b[@-Z\\-_]"  # other two-byte escapes
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SURROGATE = re.compile("[\ud800-\udfff]")
# A bare object repr: "<Figure size 640x480 with 1 Axes>", "<IPython.core.display.HTML object>".
_OBJECT_REPR = re.compile(r"^<[^\n<>]*>$")
_CHAIN = re.compile(
    r"\n(?:The above exception was the direct cause of the following exception:"
    r"|During handling of the above exception, another exception occurred:)\n"
)
_CELL_LINE = re.compile(r"Cell In\s*\[\d*\],\s*line (\d+)")
_EVALUE_LINE = re.compile(r"\bline (\d+)\)?\s*$")
_PSEUDO_FILE_LINE = re.compile(r'File "<[^>]*>", line (\d+)')
_USER_FRAME = re.compile(r"(?m)^[ \t]*Cell In\s*\[\d*\], line \d+.*\n(?:[ \t-].*\n)*")
_DASH_LINE = re.compile(r"(?m)^-{20,}$\n?")
_PACKAGES_PATH = re.compile(r"(?<=File )\S*?/((?:site|dist)-packages)/")

_IMAGE_MIMES = ("image/png", "image/jpeg")
_PLOTLY = "application/vnd.plotly.v1+json"
_WIDGET = "application/vnd.jupyter.widget-view+json"
_BUNDLE_TYPES = ("execute_result", "display_data", "update_display_data")


@dataclass
class ShapedImage:
    data: bytes
    mime: str
    width: int
    height: int
    orig: tuple[int, int]


@dataclass
class Shaped:
    text: str
    images: list[ShapedImage]
    dropped_images: int
    truncated: bool
    full_path: str | None
    error: ErrorInfo | None
    summary: OutputSummary


@dataclass
class _Image:
    number: int
    raw: bytes
    fmt: str
    orig: tuple[int, int] | None = None  # None: unreadable
    sent: ShapedImage | None = None
    skipped: str = ""  # why a readable image was not sent
    saved_as: str | None = None

    @property
    def keep_original(self) -> bool:
        return self.orig is not None and (self.sent is None or self.sent.data != self.raw)

    def line(self, *, full: bool) -> str:
        if self.orig is None:
            return f"[image {self.number}: could not be read]"
        text = f"[image {self.number}: {self.orig[0]}x{self.orig[1]} {self.fmt}"
        if self.sent is None:
            text += f", not sent ({self.skipped})"
        elif (self.sent.width, self.sent.height) != self.orig:
            text += f", sent at {self.sent.width}x{self.sent.height}"
        if full and self.saved_as:
            text += f"; original: {self.saved_as}"
        return text + "]"


@dataclass
class _Section:
    label: str | None  # rendered as "[label] "; None for image lines
    body: str = ""
    is_error: bool = False
    image: _Image | None = None

    @property
    def overhead(self) -> int:
        return len(self.label) + 3 if self.label else 0

    def render(self, body: str) -> str:
        if not self.label:
            return body
        sep = "\n" if "\n" in body else " "
        return f"[{self.label}]{sep}{body}"


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def redact(text: str) -> str:
    """FR-14 seam: secret redaction. Identity in v0.1."""
    return text


def shape_outputs(
    outputs: list[dict],
    *,
    max_chars: int,
    max_images: int,
    image_max_px: int,
    save_dir: Path | None,
) -> Shaped:
    max_images, image_max_px = max(0, max_images), max(16, image_max_px)
    sections: list[_Section] = []
    images: list[_Image] = []
    error: ErrorInfo | None = None

    for out in _coalesce_streams(outputs):
        otype = out.get("output_type")
        if otype == "stream":
            sections.append(
                _Section(str(out.get("name") or "stdout"), _clean(_text(out.get("text"))))
            )
        elif otype == "error":
            info = _error_info(out)
            error = error or info
            sections.append(_Section("error", _traceback_body(info), is_error=True))
        elif otype in _BUNDLE_TYPES:
            data = _bundle(out)
            mime = next((m for m in _IMAGE_MIMES if data.get(m)), None)
            text = _bundle_text(data, has_image=mime is not None)
            if text is not None:
                sections.append(_Section("out" if otype == "execute_result" else "display", text))
            if mime is not None:
                send = sum(1 for i in images if i.sent) < max_images
                image = _take_image(
                    data[mime], mime, len(images) + 1, send, max_images, image_max_px
                )
                images.append(image)
                sections.append(_Section(None, image=image))
        else:
            sections.append(_Section(None, f"[{otype or 'unknown'} output]"))

    saved_images = _save_originals(images, save_dir)
    for section in sections:
        if section.image is not None:
            section.body = section.image.line(full=False)
    plain = "\n".join(s.render(s.body) for s in sections)

    full_path = None
    if save_dir is not None and (len(plain) > max_chars or saved_images):
        full = "\n".join(s.render(s.image.line(full=True) if s.image else s.body) for s in sections)
        data = full.encode("utf-8", "replace")
        full_path = _write_once(save_dir, _sha_name(data, "txt"), data)
        prune_outputs_dir(save_dir)

    footer = f"[full output: {full_path}]" if full_path else ""
    budget = max_chars - (len(footer) + 1 if footer else 0)
    truncated = len(plain) > budget
    text = _fit(sections, budget) if truncated else plain
    if footer:
        text = f"{text}\n{footer}" if text else footer
    if len(text) > max_chars:  # only reachable with a budget smaller than the fixed labels
        text, truncated = text[: max(0, max_chars)], True

    sent = [i.sent for i in images if i.sent is not None]
    return Shaped(
        text=text,
        images=sent,
        dropped_images=len(images) - len(sent),
        truncated=truncated,
        full_path=full_path,
        error=error,
        summary=summarize_outputs(outputs),
    )


def summarize_outputs(outputs: list[dict]) -> OutputSummary:
    """A one-glance summary for outlines and logs. Never decodes images."""
    summary = OutputSummary(count=len(outputs))
    for out in outputs:
        if not isinstance(out, dict):
            continue
        otype = out.get("output_type")
        text = ""
        if otype == "stream":
            kind = str(out.get("name") or "stdout")
            text = _text(out.get("text"))
        elif otype == "error":
            kind = "error"
            if summary.error is None:
                ename = _clean(str(out.get("ename") or "Error"))
                summary.error = error_summary(ename, _clean(str(out.get("evalue") or "")))
        elif otype in _BUNDLE_TYPES:
            data = _bundle(out)
            kind = _primary_mime(data)
            if any(data.get(m) for m in _IMAGE_MIMES):
                summary.images += 1
            plain = _text(data.get("text/plain"))
            text = "" if _OBJECT_REPR.match(plain.strip()) else plain
        else:
            kind = str(otype or "unknown")
        if kind not in summary.types:
            summary.types.append(kind)
        if not summary.text_head and text.strip():
            summary.text_head = _first_line(text, 80)
    return summary


def prune_outputs_dir(save_dir: Path, max_files: int = 200, max_bytes: int = 50 * 2**20) -> None:
    """Delete the least recently written files beyond ``max_files`` or ``max_bytes``."""
    try:
        entries = list(os.scandir(save_dir))
    except OSError:
        return
    files: list[tuple[float, int, str]] = []
    for entry in entries:
        if entry.name.startswith("."):  # in-flight temp files
            continue
        try:
            if entry.is_file(follow_symlinks=False):
                stat = entry.stat(follow_symlinks=False)
                files.append((stat.st_mtime, stat.st_size, entry.path))
        except OSError:
            continue
    files.sort(reverse=True)
    total = 0
    for index, (_mtime, size, path) in enumerate(files):
        total += size
        if index >= max_files or total > max_bytes:
            with contextlib.suppress(OSError):
                os.unlink(path)


# --- text -----------------------------------------------------------------------------------


def _text(value: Any) -> str:
    """nbformat stores multi-line strings either whole or as a list of lines."""
    if isinstance(value, list):
        return "".join(str(part) for part in value)
    return value if isinstance(value, str) else ""


def _bundle(out: dict) -> dict:
    data = out.get("data")
    return data if isinstance(data, dict) else {}


def _collapse_cr(text: str) -> str:
    """Progress bars: keep what a terminal shows, the text after the last \\r on each line."""
    if "\r" not in text:
        return text
    lines = text.replace("\r\n", "\n").split("\n")
    return "\n".join(line.rstrip("\r").rsplit("\r", 1)[-1] for line in lines)


def _clean(text: str) -> str:
    text = _collapse_cr(strip_ansi(text))
    text = _SURROGATE.sub("\ufffd", _CONTROL.sub("", text))
    return redact(text.rstrip("\n"))


def _first_line(text: str, limit: int) -> str:
    line = next((ln.strip() for ln in _clean(text[:4000]).splitlines() if ln.strip()), "")
    return clip(line, limit)


def _coalesce_streams(outputs: Iterable[Any]) -> list[dict]:
    merged: list[dict] = []
    for out in outputs:
        if not isinstance(out, dict):
            continue
        prev = merged[-1] if merged else None
        if (
            out.get("output_type") == "stream"
            and prev is not None
            and prev.get("output_type") == "stream"
            and prev.get("name") == out.get("name")
        ):
            merged[-1] = {**prev, "text": _text(prev.get("text")) + _text(out.get("text"))}
        else:
            merged.append(out)
    return merged


def _primary_mime(data: dict) -> str:
    for mime in (*_IMAGE_MIMES, _PLOTLY, _WIDGET):
        if data.get(mime):
            return mime
    plain = _text(data.get("text/plain"))
    if plain and not _OBJECT_REPR.match(plain.strip()):
        return "text/plain"
    for mime in ("text/markdown", "text/latex", "text/html", "application/json", "image/svg+xml"):
        if mime in data:
            return mime
    return "text/plain" if plain else next(iter(data), "empty")


def _bundle_text(data: dict, *, has_image: bool) -> str | None:
    """The text shown for one mime bundle; None when its image says it all."""
    if data.get(_PLOTLY) is not None:
        figure = data[_PLOTLY]
        traces = figure.get("data") if isinstance(figure, dict) else None
        count = len(traces) if isinstance(traces, list) else 0
        return f"[plotly figure: {count} trace{'' if count == 1 else 's'}]"
    if data.get(_WIDGET) is not None:
        return "[widget]"
    # A bare object repr says less than the richer mime types below it.
    plain = _text(data.get("text/plain"))
    if plain and not _OBJECT_REPR.match(plain.strip()):
        return _clean(plain)
    for mime in ("text/markdown", "text/latex"):
        if data.get(mime):
            return _clean(_text(data[mime]))
    if has_image:
        return None
    if data.get("text/html"):
        return _clean(_html_to_text(_text(data["text/html"])))
    if "application/json" in data:
        value = data["application/json"]
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        return _clean(value)
    if data.get("image/svg+xml"):
        return "[SVG figure omitted]"
    if plain:
        return _clean(plain)
    mime = next(iter(data), None)
    return f"[{mime} output omitted]" if mime else None


class _HtmlText(HTMLParser):
    """HTML to plain text; table rows become tab-separated lines."""

    _SKIP = frozenset({"script", "style", "head", "template"})
    _BLOCK = frozenset(
        {"p", "div", "pre", "section", "article", "blockquote", "hr", "caption", "table"}
        | {"li", "ul", "ol", "dl", "dt", "dd", "h1", "h2", "h3", "h4", "h5", "h6"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0
        self.pre = 0
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self.skip += 1
        elif tag in ("td", "th"):
            self._close_cell()
            self.row = [] if self.row is None else self.row
            self.cell = []
        elif tag == "tr":
            self._close_row()
            self.row = []
        elif tag == "br":
            self.parts.append("\n")
        elif tag in self._BLOCK:
            self.pre += tag == "pre"
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("td", "th"):
            self._close_cell()
        elif tag in ("tr", "table"):
            self._close_row()
        elif tag in self._BLOCK:
            self.pre = max(0, self.pre - (tag == "pre"))
            self._newline()

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif self.row is None:  # inside a row but outside a cell is layout whitespace
            self.parts.append(data if self.pre else re.sub(r"\s+", " ", data))

    def _close_cell(self) -> None:
        if self.cell is not None and self.row is not None:
            self.row.append(" ".join("".join(self.cell).split()))
        self.cell = None

    def _close_row(self) -> None:
        self._close_cell()
        if self.row is not None:
            self._newline()
            self.parts.append("\t".join(self.row) + "\n")
        self.row = None

    def _newline(self) -> None:
        if self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def text(self) -> str:
        self._close_row()
        lines = [line.rstrip(" ") for line in "".join(self.parts).split("\n")]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _html_to_text(markup: str) -> str:
    parser = _HtmlText()
    parser.feed(markup[:MAX_HTML_CHARS])
    parser.close()
    text = parser.text()
    return text + "\n[… HTML cut …]" if len(markup) > MAX_HTML_CHARS else text


# --- errors ---------------------------------------------------------------------------------


def _error_info(out: dict) -> ErrorInfo:
    ename = _clean(str(out.get("ename") or "Error"))
    evalue = _clean(str(out.get("evalue") or ""))
    frames = out.get("traceback")
    frames = [frames] if isinstance(frames, str) else frames if isinstance(frames, list) else []
    traceback = "\n".join(_clean(str(frame)) for frame in frames)
    return ErrorInfo(
        ename=ename, evalue=evalue, traceback=traceback, line=_error_line(traceback, evalue)
    )


def _error_line(traceback: str, evalue: str) -> int | None:
    """The failing line in the cell: the outermost ``Cell In[n], line N`` of the last exception.

    Falls back to ``line N`` in a SyntaxError's value or in a ``File "<...>"`` pseudo-file frame.
    """
    last = _CHAIN.split(traceback)[-1]
    for pattern, text in (
        (_CELL_LINE, last),
        (_CELL_LINE, traceback),
        (_EVALUE_LINE, evalue),
        (_PSEUDO_FILE_LINE, last),
    ):
        match = pattern.search(text)
        if match:
            return int(match.group(1))
    return None


def _traceback_body(info: ErrorInfo) -> str:
    """The traceback without its dashed rules, ending in 'ename: evalue' exactly once."""
    text = _PACKAGES_PATH.sub(r"…/\1/", _DASH_LINE.sub("", info.traceback))
    text = re.sub(r"\n{2,}", "\n", text).strip("\n")
    headline = f"{info.ename}: {info.evalue}" if info.evalue else info.ename
    if not text:
        return headline
    if not _ends_with_error(text, info.ename, headline):
        text += "\n" + headline
    return text


def _ends_with_error(text: str, ename: str, headline: str) -> bool:
    """True when the traceback already closes with the error, even a multi-line message."""
    squashed, message = " ".join(text.split()), " ".join(headline.split())
    if squashed.endswith(message) or text.rsplit("\n", 1)[-1].lstrip().startswith(ename):
        return True
    first = " ".join(headline.split("\n", 1)[0].split())
    tail = text[-(len(headline) + 200) :]
    return any(" ".join(line.split()) == first for line in tail.splitlines())


# --- budget ---------------------------------------------------------------------------------


def _cut_marker(count: int) -> str:
    return f"\n[… {count:,} chars cut …]\n"


def _cut(text: str, limit: int) -> str:
    """Keep the head (30%) and tail (70%), snapped to line breaks where that costs little."""
    if len(text) <= limit:
        return text
    room = limit - len(_cut_marker(len(text)))
    if room <= 0:
        return _cut_marker(len(text)).strip()[: max(0, limit)]
    head_len = int(room * HEAD_SHARE)
    head, tail = text[:head_len], text[len(text) - (room - head_len) :]
    newline = head.rfind("\n")
    if newline >= head_len * 0.6:
        head = head[:newline]
    newline = tail.find("\n")
    if 0 <= newline <= len(tail) * 0.4:
        tail = tail[newline + 1 :]
    return head + _cut_marker(len(text) - len(head) - len(tail)) + tail


def _cut_traceback(text: str, limit: int) -> str:
    """Tracebacks keep the tail (the error) plus the frame in the user's cell when it fits."""
    if len(text) <= limit:
        return text
    room = limit - len(_cut_marker(len(text)))
    if room <= 0:
        return _cut(text, limit)
    chained = list(_CHAIN.finditer(text))
    last_exception = chained[-1].end() if chained else 0
    frame = _USER_FRAME.search(text, last_exception) or _USER_FRAME.search(text)
    head, tail_start = "", len(text) - room
    if frame and frame.start() < tail_start:
        head = frame.group(0).rstrip("\n")[: int(limit * 0.45)]
        tail_start = max(len(text) - (room - len(head)), frame.end())
    tail = text[tail_start:]
    newline = tail.find("\n")
    if 0 <= newline <= len(tail) * 0.3:
        tail = tail[newline + 1 :]
    marker = _cut_marker(len(text) - len(head) - len(tail))
    return head + marker + tail if head else marker.lstrip("\n") + tail


def _water_fill(lengths: list[int], budget: int) -> list[int]:
    """Max-min fair shares: short outputs stay whole, long ones split what is left."""
    shares = [0] * len(lengths)
    left = max(0, budget)
    order = sorted(range(len(lengths)), key=lengths.__getitem__)
    for rank, index in enumerate(order):
        shares[index] = min(lengths[index], left // (len(order) - rank))
        left -= shares[index]
    return shares


def _fit(sections: list[_Section], budget: int) -> str:
    """Cut sections so the joined text fits ``budget``; an error gets most of it when present.

    If even ``MIN_SHARE`` characters each is too much, outputs from the middle are left out.
    """
    errors = [i for i, s in enumerate(sections) if s.is_error]
    others = [i for i, s in enumerate(sections) if not s.is_error]
    cost = [s.overhead + 1 for s in sections]  # label plus the joining newline
    avail = budget - sum(cost[i] for i in errors)
    error_len = sum(len(sections[i].body) for i in errors)
    other_budget = avail - (min(error_len, int(avail * ERROR_SHARE)) if others else error_len)

    marker_cost = len(f"[… {len(others):,} more outputs not shown …]") + 1
    need = {i: cost[i] + min(len(sections[i].body), MIN_SHARE) for i in others}
    kept = others
    if sum(need.values()) > other_budget:
        kept, spent = [], marker_cost
        for i in _front_back(others):
            if spent + need[i] > other_budget:
                break
            kept.append(i)
            spent += need[i]
        kept.sort()
    kept_set = set(kept)
    omitted = [i for i in others if i not in kept_set]
    fixed = sum(cost[i] for i in kept) + (marker_cost if omitted else 0)
    lengths = [len(sections[i].body) for i in kept]
    shares = dict(zip(kept, _water_fill(lengths, other_budget - fixed), strict=True))
    error_budget = avail - fixed - sum(shares.values())
    lengths = [len(sections[i].body) for i in errors]
    shares.update(zip(errors, _water_fill(lengths, error_budget), strict=True))

    parts: list[str] = []
    for index, section in enumerate(sections):
        if index in shares:
            cut = _cut_traceback if section.is_error else _cut
            parts.append(section.render(cut(section.body, shares[index])))
        elif omitted and index == omitted[0]:
            parts.append(f"[… {len(omitted):,} more outputs not shown …]")
    return "\n".join(parts)


def _front_back(indices: list[int]) -> list[int]:
    """First, last, second, second-to-last, …: what survives is the head and the tail."""
    order: list[int] = []
    lo, hi = 0, len(indices) - 1
    while lo <= hi:
        order.append(indices[lo])
        if hi != lo:
            order.append(indices[hi])
        lo, hi = lo + 1, hi - 1
    return order


# --- images ---------------------------------------------------------------------------------


def _take_image(
    value: Any, mime: str, number: int, send: bool, max_images: int, max_px: int
) -> _Image:
    fmt = mime.split("/", 1)[1]
    try:
        raw = base64.b64decode(_text(value))
        with Image.open(io.BytesIO(raw)) as img:
            image = _Image(number, raw, (img.format or fmt).lower(), orig=img.size)
            if not send:
                image.skipped = f"limit {max_images}"
            elif img.width * img.height > MAX_DECODE_PIXELS:
                image.skipped = "too large to decode"
            else:
                image.sent = _fit_image(img, raw, max_px)
            return image
    except Exception:  # corrupt or unsupported data: base64, Pillow and codec errors of many types
        return _Image(number, b"", fmt)


def _fit_image(img: Image.Image, raw: bytes, max_px: int) -> ShapedImage:
    """≤ max_px on the long side; PNG, or JPEG q80 when the PNG would exceed 150 KB."""
    orig, fmt = img.size, img.format
    if max(orig) <= max_px and len(raw) <= PNG_LIMIT and fmt in ("PNG", "JPEG"):
        return ShapedImage(raw, f"image/{fmt.lower()}", orig[0], orig[1], orig)
    work = _resizable(img)
    work.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    if fmt != "JPEG":
        # Default zlib level, not optimize=True: on matplotlib figures that was ~5x slower and
        # no smaller.
        png = _encode(work, "PNG")
        if len(png) <= PNG_LIMIT:
            return ShapedImage(png, "image/png", work.width, work.height, orig)
    jpeg = _encode(_flatten(work), "JPEG", quality=80, optimize=True)
    return ShapedImage(jpeg, "image/jpeg", work.width, work.height, orig)


def _resizable(img: Image.Image) -> Image.Image:
    """The image, converted when PNG or LANCZOS resampling can't take its mode.

    Resizing in place is fine: the image was opened from bytes for this call only, and an
    unloaded JPEG then decodes at reduced scale (Pillow's draft mode).
    """
    if img.mode in ("RGB", "RGBA", "L", "LA"):
        return img
    if img.mode in ("P", "PA") or "A" in img.getbands() or "transparency" in img.info:
        return img.convert("RGBA")
    return img.convert("L" if img.mode in ("1", "I", "I;16", "F") else "RGB")


def _flatten(img: Image.Image) -> Image.Image:
    """JPEG has no alpha: composite on white."""
    if img.mode not in ("RGBA", "LA"):
        return img
    background = Image.new("RGB", img.size, "white")
    background.paste(img.convert("RGB"), mask=img.getchannel("A"))
    return background


def _encode(img: Image.Image, fmt: str, **options: Any) -> bytes:
    buffer = io.BytesIO()
    img.save(buffer, fmt, **options)
    return buffer.getvalue()


# --- files ----------------------------------------------------------------------------------


def _sha_name(data: bytes, ext: str) -> str:
    return f"{hashlib.sha256(data).hexdigest()[:16]}.{ext}"


def _save_originals(images: list[_Image], save_dir: Path | None) -> bool:
    if save_dir is None:
        return False
    for image in images:
        if image.keep_original:
            ext = "jpg" if image.fmt in ("jpeg", "jpg") else image.fmt
            image.saved_as = _write_once(save_dir, _sha_name(image.raw, ext), image.raw)
    return any(image.saved_as for image in images)


def _write_once(save_dir: Path, name: str, data: bytes) -> str | None:
    """Content-addressed write; an existing copy is only touched (LRU). None if the disk fails."""
    path = save_dir / name
    try:
        if path.exists():
            os.utime(path)
        else:
            save_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=str(save_dir))
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                os.replace(tmp, path)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
    except OSError:
        return None
    return _project_relative(path)


def _project_relative(path: Path) -> str:
    """'.nh/outputs/<name>' when the path is inside an nh project, else the absolute path."""
    parts = path.parts
    if ".nh" in parts:
        start = len(parts) - 1 - parts[::-1].index(".nh")
        return "/".join(parts[start:])
    return path.as_posix()
