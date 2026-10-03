"""a7 (design §6.13): very large outputs through the real RTC path, against a real JupyterLab.

Spike V11's two cases, and both in one cell, each added in one message and re-run in the next
nine (10 calls a case):

- **50 MB stream:** ten 5 MB writes of a training log. The project's ``.env`` holds ~30 secret
  values, as in V11; the stream carries one of them (in every write) and a GitHub token (in the
  fifth and the last).
- **20 MB image bundle:** a short stream, then four noise PNGs of ~5 MB of base64 each, the first
  just over docsafe's 5 MB bundle cap.
- **Both in one cell:** the stream, then the images, in one output list. Before the trim its
  full copy (~50 MB) and image originals were over ``.nh/outputs``' 50 MiB cap together, which
  exposed the prune that deleted a call's own originals; trimmed (~8 MB and ~15 MB), they are
  under it, and ``test_shaping.py`` keeps that case (``test_a_call_s_own_copies_survive_its_prune``).

A first message adds a small cell with the helpers (so the kernel is up and the gateway warm). It
also has IPython record each later cell's own run time (``pre_run_cell`` to ``post_run_cell``).

The test starts its own JupyterLab with jupyter_server's iopub data limit raised, as users do
when they print this much: at its default (1 MB/s over a 3 s window) the server drops any stream
message over 3 MB before it reaches a client, nh included, and sends its "IOPub data rate
exceeded" notice instead, so nothing large would reach nh at all.

Checked, for each case:
- the room (the user's tab) and the saved ``.ipynb`` hold docsafe's capped outputs: the stream's
  last 1 MB, no bundle over 5 MB (the first image becomes nh's note), and at most 8 MB per cell
  (the images past it become one notice);
- Claude's tool result: the output section fits ``[output] max_chars`` and ends with
  ``[full output: …]``, at most ``max_images`` images go with it, and every image original the
  full copy names is on disk;
- the trim (design §6.8): the full copy under ``.nh/outputs`` holds the stream's raw head and
  tail, at most ``TRIM_CHARS`` in all, and one ``[… N chars cut …]`` line whose N is the rest
  (the raw chars it didn't keep); the result's own cut marker counts the whole stream too (what
  the copy holds, the trim marker as its N, less what the result shows);
- C3's redaction ran on all that is kept: no planted secret in the result or the full copy, and
  the kept head and tail are the raw stream's with each planted secret as its marker;
- time, per call, against the 1.5 s budget (design §6.8, spike V11), p95 by nearest rank over
  10 calls a case (which is the max of the 10), for V11's two cases, which §6.8 names. The
  combined one is reported without a budget: it does both cases' work in one call (a trimmed
  stream, four image decodes and resizes, 15 MB of originals) and the room's saves of a 12 MB
  notebook, and in C12's review it measured 1.24-1.58 s (p95 1.56-1.58 s, over) at load 1.8-2.6:
  - turn overhead: the call's wall time minus the run's ``exec.ms`` (its ``cell_added``/
    ``cell_rerun`` event: from nh seeing the kernel start the cell until nh holds every
    output). That is the gateway's work before the run and after it (the after-probe,
    ``shape_outputs``, render) and the MCP round trip: V11 timed the same work as
    ``harness_ms`` plus ``shape_outputs``. It is not what ``nhctl metrics`` reports for these
    calls: the event's ``harness_ms`` is the pre-run part only (``write.py`` takes it before
    waiting for the run), and ``nhctl metrics`` reads that field first, falling back to
    total − exec only for events without it. ``shape_outputs``' share, ``harness_ms`` and the
    load average are printed with it;
  - the receive path, which that measure leaves out: ``exec.ms`` minus the kernel's own run
    time, the time the outputs take to reach nh (before nh checked the kernel websocket's
    UTF-8 in C, ~10 s per 50 MB).

The gateway logs at INFO into the project's ``.nh/logs/gateway.log`` (CI keeps it), with each
REST call that takes ``SLOW_REST_S`` or more, and the report lists those calls and each kernel
connection nh replaced because it didn't answer its check, or used when it answered late. A ~5 s
pre-run stall (``harness_ms`` ~5050) failed this test in 2 of 9 runs during C12's review and 3
of 6 after it: the probe client nh opens after each run was dead from the start, and the next
call's attach and before-probes waited out their timeouts on it (design §6.13, dead kernel
connections; fixed in ``kernel.open_client``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import random
import re
import string
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import nbformat
import pytest
from fastmcp import Client

from nh_gateway._shared.paths import Layout
from nh_gateway.app import create_server
from nh_gateway.backend import rest
from nh_gateway.backend.rtc_backend import RtcBackend
from nh_gateway.config import ConfigCache
from nh_gateway.exec import docsafe, shaping
from tests.fakes.turns import Turns, text

pytestmark = pytest.mark.integration

NB = "notebooks/01_eda.ipynb"
BUDGET_S = 1.5  # turn overhead p95 (design §6.8)
SLOW_REST_S = 1.0  # a REST call this slow goes to gateway.log (a stalled call)
REPLACED = "connecting again"  # kernel.open_client's INFO line for a connection it replaced
LATE = "kernel_info_request late"  # ... and for an earlier one that answered late, which it used
MAX_CHARS = 2000  # [output] max_chars, the default
MAX_IMAGES = 2  # [output] max_images, the default
UNLIMITED = ("--ZMQChannelsWebsocketConnection.iopub_data_rate_limit=1e10",)
FOOTER = re.compile(r"\[full output: \.nh/outputs/[0-9a-f]{16}\.txt\]")
CUT = re.compile(r"\n\[… ([\d,]+) chars cut …\]\n")


def env_values() -> list[tuple[str, str]]:
    """~30 secret-named values of 16-45 chars, as V11's project .env holds them."""
    rng = random.Random(7)
    names = ["DB_PASSWORD", "API_TOKEN", "AWS_SECRET_ACCESS_KEY", "WAREHOUSE_PASSWORD"]
    names += [f"SERVICE_{i}_API_KEY" for i in range(26)]
    alphabet = string.ascii_letters + string.digits
    return [(name, "".join(rng.choices(alphabet, k=16 + i))) for i, name in enumerate(names)]


VALUES = env_values()
PASSWORD = VALUES[0][1]
GITHUB = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # fake
TOKEN_PARTS = (4, 9)  # the writes that also print the token
LINE = "epoch 12/100 - loss: 0.2345 - acc: 0.9123 - see https://example.com/x?a=1\n"  # V11's
MARKERS = {"[redacted:DB_PASSWORD]": PASSWORD, "[redacted:github-token]": GITHUB}


def helpers_code(times: Path) -> str:
    """The first cell. Each secret is built from two halves: no line of code holds it whole.
    IPython appends each later cell's own run time to ``times`` (nh's silent probes don't count)."""
    half, gh = len(PASSWORD) // 2, len(GITHUB) // 2
    return f"""import base64
import json
import os
import struct
import time
import zlib

from IPython.display import display

LINE = {LINE!r}
SECRET = {PASSWORD[:half]!r} + {PASSWORD[half:]!r}
TOKEN = {GITHUB[:gh]!r} + {GITHUB[gh:]!r}
TIMES = {str(times)!r}
began = [0.0]


def noise_png(side):
    rows = b"".join(b"\\x00" + os.urandom(side * 3) for _ in range(side))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    png = chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows, 0)) + chunk(b"IEND", b"")
    return b"\\x89PNG\\r\\n\\x1a\\n" + png


def cell_started(info):
    began[0] = time.perf_counter()


def cell_ran(result):
    if began[0]:
        record = {{"code": result.info.raw_cell[:40], "s": time.perf_counter() - began[0]}}
        with open(TIMES, "a") as log:
            log.write(json.dumps(record) + "\\n")


get_ipython().events.register("pre_run_cell", cell_started)
get_ipython().events.register("post_run_cell", cell_ran)
print("helpers ready")"""


# 10 x 5 MB of log lines; the .env value ends every write, the token the fifth and the last.
STREAM = f"""block = LINE * (5_000_000 // len(LINE))
for part in range(10):
    extra = f" key {{TOKEN}}" if part in {TOKEN_PARTS} else ""
    print(block + f"part {{part}} db {{SECRET}}{{extra}}", flush=True)
del block"""

# base64 of ~5.67 MB (over the 5 MB bundle cap), then three of ~4.80 MB: 20 MB in all.
IMAGES = """print(f"four figures, db {SECRET}")
for side in (1190, 1095, 1095, 1095):
    data = base64.b64encode(noise_png(side)).decode()
    display({"image/png": data, "text/plain": f"<Figure {side}x{side}>"}, raw=True)
del data"""

BOTH = "# both in one cell\n" + STREAM + "\n" + IMAGES
IMAGES_LINE = f"four figures, db {PASSWORD}\n"  # what IMAGES prints before its figures


def stream_text() -> str:
    """The stream STREAM prints, as nh receives it."""
    block = LINE * (5_000_000 // len(LINE))
    return "".join(
        f"{block}part {part} db {PASSWORD}"
        + (f" key {GITHUB}" if part in TOKEN_PARTS else "")
        + "\n"
        for part in range(10)
    )


def cell(code: str, title: str) -> dict[str, Any]:
    return dict(
        title=title,
        notes=["Makes very large outputs on purpose.", "Checks what nh keeps and shows."],
        intent="stress nh with very large outputs",
        code=code,
    )


def run_event(project: Path, uid: str) -> dict:
    lines = (project / ".nh" / "log.jsonl").read_text().splitlines()
    events = [json.loads(line) for line in lines if line.strip()]
    return [
        e for e in events if e["event"] in ("cell_added", "cell_rerun") and e.get("cell_uid") == uid
    ][-1]


def kernel_seconds(times: Path, code: str) -> float:
    """The cell's own run time, as IPython saw it (``times`` holds this call's records only)."""
    records = [json.loads(line) for line in times.read_text().splitlines() if line.strip()]
    [record] = [r for r in records if r["code"] == code[:40]]
    return float(record["s"])


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def p95(values: list[float]) -> str:
    value = nearest_rank(values, 0.95)
    the_max = ": the max" if value == max(values) else ""
    return f"p95 {value:.3f}s (nearest rank of {len(values)}{the_max})"


def section(body: str, name: str) -> str:
    match = re.search(rf"^--- {name} ---\n(.*?)(?=^--- [^\n]+ ---$|\Z)", body, re.M | re.S)
    assert match, f"no {name} section in:\n{body[:3000]}"
    return match.group(1).rstrip("\n")


def joined(value: str | list[str]) -> str:
    """The saved .ipynb splits multi-line strings into lists of lines; the room doesn't."""
    return "".join(value) if isinstance(value, list) else value


# --- what Claude sees ------------------------------------------------------------------------


def cut_output(body: str) -> tuple[str, str]:
    """The output section (within max_chars, ending with the full-copy line) and that path."""
    output = section(body, "output")
    assert len(output) <= MAX_CHARS, len(output)
    footer = output.splitlines()[-1]
    assert FOOTER.fullmatch(footer), footer
    return output, footer[len("[full output: ") : -1]


def attached(result: Any) -> int:
    return len([part for part in result.content if getattr(part, "type", "") == "image"])


def check_originals(project: Path, full: str, count: int) -> None:
    """Every image original the full copy names is on disk (a call's own copies are never pruned
    by that call)."""
    originals = re.findall(r"; original: (\.nh/outputs/[0-9a-f]{16}\.png)\]", full)
    assert len(originals) == count, originals
    missing = [name for name in originals if not (project / name).is_file()]
    assert not missing, f"the full copy names originals that are gone: {missing}"


def check_image_lines(output: str) -> None:
    assert "[image 1: 1190x1190 png, sent at 768x768]" in output, output
    assert "[image 2: 1095x1095 png, sent at 768x768]" in output, output
    assert "[image 4: 1095x1095 png, not sent (limit 2)]" in output, output


def restore(text: str) -> str:
    """Kept text with each planted secret back in place of its marker."""
    for marker, secret in MARKERS.items():
        text = text.replace(marker, secret)
    return text


def cut_count(match: re.Match[str]) -> int:
    return int(match.group(1).replace(",", ""))


def check_trimmed(kept: str, raw: str, output: str) -> None:
    """The trim (design §6.8): the full copy keeps ``raw``'s head and tail, at most TRIM_CHARS,
    exactly as printed but for each planted secret, which is its marker wherever it is kept, and
    one trim marker counting the rest (no secret is near the windows' cuts here, so that is the
    raw length minus what is kept, secrets restored). The result's own cut marker counts what the
    copy holds, its trim marker as the chars it stands for, less what the result shows."""
    last = "part 9 db [redacted:DB_PASSWORD] key [redacted:github-token]"
    raw = raw.rstrip("\n")
    assert len(kept) <= shaping.TRIM_CHARS and PASSWORD not in kept and GITHUB not in kept
    [trim] = CUT.finditer(kept)
    head, tail = kept[: trim.start()], kept[trim.end() :]
    assert raw.startswith(restore(head)) and raw.endswith(restore(tail))
    assert cut_count(trim) == len(raw) - len(restore(head)) - len(restore(tail))
    assert last in tail and tail.count("[redacted:DB_PASSWORD]") >= 2
    output = output.rsplit("\n[full output: ", 1)[0]
    [cut] = CUT.finditer(output)
    shown_head, shown_tail = output[len("[stdout]\n") : cut.start()], output[cut.end() :]
    shown_tail = shown_tail.split("\n[image 1: ")[0]
    assert raw.startswith(restore(shown_head)) and last in shown_tail
    held = len(head) + cut_count(trim) + len(tail)
    assert cut_count(cut) == held - len(shown_head) - len(shown_tail)


def check_stream_result(project: Path, body: str, result: Any) -> None:
    output, copy = cut_output(body)
    assert PASSWORD not in body and GITHUB not in body
    assert attached(result) == 0
    full = (project / copy).read_text()
    assert full.startswith("[stdout]\n"), full[:200]
    check_trimmed(full[len("[stdout]\n") :], stream_text(), output)


def check_images_result(project: Path, body: str, result: Any) -> None:
    output, copy = cut_output(body)
    assert "four figures, db [redacted:DB_PASSWORD]" in output, output
    check_image_lines(output)
    assert PASSWORD not in body
    assert attached(result) == MAX_IMAGES
    full = (project / copy).read_text()
    assert PASSWORD not in full and full.count("[redacted:DB_PASSWORD]") == 1
    check_originals(project, full, 4)


def check_both_result(project: Path, body: str, result: Any) -> None:
    output, copy = cut_output(body)
    assert "four figures, db [redacted:DB_PASSWORD]" in output, output
    check_image_lines(output)
    assert PASSWORD not in body and GITHUB not in body
    assert attached(result) == MAX_IMAGES
    full = (project / copy).read_text()
    assert full.startswith("[stdout]\n"), full[:200]
    stream = full[len("[stdout]\n") : full.index("\n[image 1: ")]
    assert stream.endswith("\nfour figures, db [redacted:DB_PASSWORD]")
    check_trimmed(stream, stream_text() + IMAGES_LINE, output)
    check_originals(project, full, 4)


# --- what the notebook keeps -----------------------------------------------------------------


def check_capped_stream(stream: dict, where: str, ending: str) -> None:
    """docsafe's stream cap: the notebook keeps the last 1 MB, raw, after nh's notice line."""
    assert stream["output_type"] == "stream", where
    notice, _, tail = joined(stream["text"]).partition("\n")
    assert re.fullmatch(
        r"\[nh: [\d,]+ earlier characters of this output were not saved in the notebook\]", notice
    ), (where, notice)
    assert 0 < len(tail) <= docsafe.STREAM_KEEP_CHARS, (where, len(tail))
    assert tail.endswith(ending), where  # the notebook keeps the raw text


def check_capped_images(outputs: list[dict], where: str) -> None:
    """docsafe's bundle (5 MB) and cell (8 MB) caps on ``outputs[1:]``, after the stream."""
    note, image, notice = outputs[1:]
    plain = joined(note["data"]["text/plain"])
    assert re.fullmatch(
        r"\[nh: this output \(5\.\d MB\) is too large to save in the notebook\]", plain
    ), (where, plain)
    assert "image/png" not in note["data"], where
    assert joined(image["data"]["text/plain"]) == "<Figure 1095x1095>", where
    assert docsafe.approx_size(image) <= docsafe.BUNDLE_MAX_CHARS, where
    assert notice["output_type"] == "stream" and notice["name"] == "stderr", where
    assert joined(notice["text"]) == (
        "[nh: 2 more output(s) were not saved; this cell's output is over 8 MB]\n"
    ), where
    assert sum(docsafe.approx_size(o) for o in outputs) <= docsafe.CELL_MAX_CHARS, where


def check_stream_caps(outputs: list[dict], where: str) -> None:
    [stream] = outputs
    check_capped_stream(stream, where, f"part 9 db {PASSWORD} key {GITHUB}\n")


def check_image_caps(outputs: list[dict], where: str) -> None:
    assert len(outputs) == 4, (where, len(outputs))
    assert joined(outputs[0]["text"]) == f"four figures, db {PASSWORD}\n", where
    check_capped_images(outputs, where)


def check_both_caps(outputs: list[dict], where: str) -> None:
    assert len(outputs) == 4, (where, len(outputs))
    check_capped_stream(outputs[0], where, f"part 9 db {PASSWORD} key {GITHUB}\n{IMAGES_LINE}")
    check_capped_images(outputs, where)


Check = Callable[..., None]
CASES: dict[str, tuple[str, int, bool, Check, Check]] = {
    # case: (code, calls: one add and the rest re-runs, budgeted, result check, notebook check)
    "50 MB stream": (STREAM, 10, True, check_stream_result, check_stream_caps),
    "20 MB image bundle": (IMAGES, 10, True, check_images_result, check_image_caps),
    "both in one cell": (BOTH, 10, False, check_both_result, check_both_caps),  # reported
}


@contextlib.asynccontextmanager
async def unlimited_lab(helpers, base: Path, monkeypatch) -> AsyncIterator[tuple[Any, Path]]:
    """A JupyterLab of its own with the iopub data limit raised, and an nh project in it."""
    lab = helpers.start_lab(base, args=UNLIMITED)
    try:
        monkeypatch.setenv("JUPYTER_RUNTIME_DIR", str(lab.runtime))
        for var in ("NH_JUPYTER_URL", "NH_JUPYTER_TOKEN", "JUPYTER_TOKEN"):
            monkeypatch.delenv(var, raising=False)
        project = lab.root / "proj"
        (project / ".nh" / "state").mkdir(parents=True)
        helpers.new_notebook(
            project / NB, [nbformat.v4.new_markdown_cell("# Big outputs", id="title")]
        )
        (project / "harness.toml").write_text(f'version = 1\n[project]\nnotebook = "{NB}"\n')
        (project / ".env").write_text("".join(f"{name}={value}\n" for name, value in VALUES))
        yield lab, project
    finally:
        helpers.stop(lab.proc)


@pytest.fixture
def gateway_info_log(monkeypatch) -> Any:
    """The gateway's INFO records too in the project's ``.nh/logs/gateway.log`` (WARNING and up
    otherwise), which CI keeps as an artifact: each kernel connection nh replaced or used late,
    and each REST call that takes SLOW_REST_S or more, with its method, path, time and outcome,
    so a stalled call leaves a trace there."""
    logger = logging.getLogger("nh_gateway")
    level = logger.level
    logger.setLevel(logging.INFO)
    request = rest.Rest.request

    def timed(self: rest.Rest, method: str, path: str, **kwargs: Any) -> Any:
        began, outcome = time.perf_counter(), "?"
        try:
            response = request(self, method, path, **kwargs)
            outcome = str(response.status_code)
            return response
        except BaseException as exc:
            outcome = type(exc).__name__
            raise
        finally:
            took = time.perf_counter() - began
            if took >= SLOW_REST_S:
                logger.info("a7: slow REST call %s %s: %.2fs (%s)", method, path, took, outcome)

    monkeypatch.setattr(rest.Rest, "request", timed)
    yield
    logger.setLevel(level)


async def test_large_outputs_are_capped_in_the_room_and_cut_for_claude(
    helpers, tmp_path: Path, monkeypatch, gateway_info_log
) -> None:
    shaped_s: list[float] = []
    real_shape = shaping.shape_outputs

    def timed_shape(*args: Any, **kwargs: Any) -> shaping.Shaped:
        began = time.perf_counter()
        try:
            return real_shape(*args, **kwargs)
        finally:
            shaped_s.append(time.perf_counter() - began)

    monkeypatch.setattr(shaping, "shape_outputs", timed_shape)
    rows: list[str] = []
    overhead: dict[str, list[float]] = {case: [] for case in CASES}
    receive: dict[str, list[float]] = {case: [] for case in CASES}
    uids: dict[str, str] = {}
    base = Path(os.path.realpath(tmp_path / "lab"))
    async with unlimited_lab(helpers, base, monkeypatch) as (lab, project):
        # inside the project: a write outside it asks first (L013), unless it is in a temp
        # folder, and CI's basetemp isn't one
        times = project / "kernel-times.jsonl"
        turns = Turns(project, tmp_path / "data")
        api_path = (project / NB).relative_to(lab.root).as_posix()
        backend = RtcBackend(Layout(project), ConfigCache(project))
        try:
            async with (
                Client(create_server(project, backend)) as client,
                helpers.observe(lab, api_path) as user,
            ):
                turns.prompt("a7-warm")
                warm = await turns.call(
                    client,
                    "nh_add_cell",
                    cell(helpers_code(times), "Define the helpers"),
                    "a7-warm",
                )
                assert "ran ok" in text(warm), text(warm)
                for case, (code, calls, _budgeted, check_result, _caps) in CASES.items():
                    for run in range(calls):
                        prompt = f"a7-{case[:5]}-{run}"
                        turns.prompt(prompt)
                        if case in uids:
                            tool, args = "nh_run", {"cell_id": uids[case]}
                        else:
                            tool, args = "nh_add_cell", cell(code, f"Print a {case}")
                        turns.stamp(tool, args, prompt)
                        times.unlink(missing_ok=True)
                        began = time.perf_counter()
                        result = await client.call_tool(tool, args, raise_on_error=False)
                        wall = time.perf_counter() - began
                        body = text(result)
                        assert not result.is_error and "ran ok" in body, body[:3000]
                        uid = uids.setdefault(case, body.split("nh: cell=")[1].split()[0])
                        event = run_event(project, uid)
                        exec_s = event["exec"]["ms"] / 1000
                        kernel_s = kernel_seconds(times, code)
                        overhead[case].append(wall - exec_s)
                        receive[case].append(exec_s - kernel_s)
                        rows.append(
                            f"{case} run {run}: call {wall:.3f}s, kernel {kernel_s:.3f}s, "
                            f"exec {exec_s:.3f}s, receive {exec_s - kernel_s:.3f}s, "
                            f"overhead {wall - exec_s:.3f}s, call - kernel {wall - kernel_s:.3f}s, "
                            f"shape_outputs {shaped_s[-1]:.3f}s, "
                            f"harness_ms {event.get('harness_ms')}, "
                            f"load {os.getloadavg()[0]:.2f}"
                        )
                        print(f"a7 {rows[-1]}", flush=True)
                        check_result(project, body, result)

                # The room: the user's tab holds the capped outputs.
                for case, uid in uids.items():

                    def live(uid: str = uid) -> dict | None:
                        found = [c for c in helpers.cells_of(user) if c["id"] == uid]
                        idle = found and found[0].get("execution_state") == "idle"
                        return found[0] if idle else None

                    outputs = (await helpers.eventually(live, timeout=30))["outputs"]
                    CASES[case][4](outputs, case)

            # The .ipynb JupyterLab saves holds the same, once its last save caught up (a save
            # made while a cell ran holds that moment's outputs).
            def check_disk() -> dict:
                disk = helpers.read_disk(project / NB)
                assert disk is not None, "no saved notebook"
                for case, uid in uids.items():
                    [found] = [c for c in disk["cells"] if c["id"] == uid]
                    CASES[case][4](found["outputs"], f"{case} on disk")
                return disk

            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                with contextlib.suppress(AssertionError, KeyError, TypeError, ValueError):
                    check_disk()
                    break
                await asyncio.sleep(0.2)
            nbformat.validate(nbformat.from_dict(check_disk()))
        finally:
            await backend.aclose()

    lines = [f"a7 ({sys.platform}, Python {sys.version.split()[0]}):", *rows]
    gateway_log = Layout(project).logs / "gateway.log"
    logged = gateway_log.read_text().splitlines() if gateway_log.exists() else []
    lines += [line for line in logged if "a7: slow REST call" in line] or [
        f"no REST call took {SLOW_REST_S}s or more"
    ]
    lines += [line for line in logged if REPLACED in line or LATE in line] or [
        "no kernel connection was replaced"
    ]
    over: list[str] = []
    for case, (_code, _calls, budgeted, _result, _caps) in CASES.items():
        limit = f"budget {BUDGET_S}s" if budgeted else "not budgeted"
        lines.append(
            f"{case}: turn overhead (call - exec.ms) {p95(overhead[case])}; "
            f"receive (exec.ms - kernel) {p95(receive[case])}; {limit}"
        )
        if budgeted:
            over += [
                f"{case} {name}"
                for name, values in (("turn overhead", overhead[case]), ("receive", receive[case]))
                if nearest_rank(values, 0.95) > BUDGET_S
            ]
    report = "\n".join(lines)
    print(f"\n{report}")
    assert not over, f"over the {BUDGET_S}s budget: {over}\n{report}"
