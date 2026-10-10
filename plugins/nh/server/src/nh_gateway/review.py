"""``python -m nh_gateway.review flag|report``: /nh:review's analysis (design §6.10).

``nhctl fresh-run --review`` launches it in nh's runtime venv, as libexec/nh-mcp launches the
gateway: one JSON request on stdin, one JSON answer on stdout. It imports only the stdlib and nh's
own stdlib-only modules (never nbformat, fastmcp or the backends: the copy is read as JSON), so its
cold start stays small (spike V8).

- ``flag``: the code cells the gateway's lint says would install packages, reach the network,
  write outside the project or show a secret in the review's kernel, and those nh can't parse
  (D154 asks about them first), with their digest (what ``--yes`` must name).
- ``report``: failing cells, hidden-state dependencies, ``src/`` candidates and the intent summary
  of a run, as JSON and as the Markdown report nhctl writes to ``.nh/reviews/``.

Every user text it shows (labels, intents, headings, error values) is redacted with the project's
Redactor before any cut, and the whole Markdown once more at the end (design §6.8).
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from nh_gateway import config, meta
from nh_gateway._shared import hosts, paths, secrets
from nh_gateway._shared.text import normalize_title
from nh_gateway.dataflow import EVERYTHING, NAMESPACE_CELL_MAGICS, analyze, defined_names
from nh_gateway.lint.lint import kernel_provides, lint_cell
from nh_gateway.lint.magics import lines_of, mask
from nh_gateway.render import cell_label

# The rules whose cells wait for the user's yes before a review runs them, in the order `rules`
# lists them: the three that ask in the live notebook (design §6.4) and the two secret rules (§6.7).
FLAG_RULES = (
    ("L009", "package_install"),
    ("L012", "network"),
    ("L013", "outside_write"),
    ("L011", "secret_print"),
    ("L014", "secret_name"),
)
# A cell nh's Python can't parse: the rules above can't see into it, so it is asked about too.
UNREADABLE = "unreadable"
DIGEST_CHARS = 16
# A fresh run's error that points at state only the live kernel had (design §6.10).
FRESH_ONLY_ERRORS = frozenset({"NameError", "UnboundLocalError", "KeyError"})
EVALUE_CHARS = 500  # as plain fresh-run's failing cell
NOT_RUN_REASONS = ("flagged", "stopped", "timeout", "kernel_died")
_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
_EMPTY_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]*$")
_SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_NOT_PARAGRAPH = re.compile(r"^ {0,3}(?:>|[-*+](?:[ \t]|$)|\d{1,9}[.)](?:[ \t]|$))")


@dataclass
class Cell:
    """One notebook cell, as the copy's JSON holds it."""

    index: int
    kind: str
    id: str
    source: str
    count: int | None
    outputs: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def nh(self) -> dict[str, Any]:
        return meta.nh_meta(self.metadata)

    @property
    def runs(self) -> bool:
        """A code cell with source: the runner skips the others, as nbclient does."""
        return self.kind == "code" and bool(self.source.strip())

    @property
    def stored_error(self) -> bool:
        return any(isinstance(o, dict) and o.get("output_type") == "error" for o in self.outputs)


def load_cells(nb: object) -> list[Cell]:
    """The cells of a notebook's JSON; what isn't a cell reads as an empty markdown one."""
    raw = nb.get("cells") if isinstance(nb, dict) else None
    cells: list[Cell] = []
    for index, item in enumerate(raw if isinstance(raw, list) else []):
        item = item if isinstance(item, dict) else {}
        source = item.get("source", "")
        source = "".join(source) if isinstance(source, list) else str(source or "")
        count = item.get("execution_count")
        outputs = item.get("outputs")
        metadata = item.get("metadata")
        cells.append(
            Cell(
                index=index,
                kind=str(item.get("cell_type") or "markdown"),
                id=str(item.get("id") or ""),
                source=source,
                count=count if isinstance(count, int) and not isinstance(count, bool) else None,
                outputs=[o for o in outputs if isinstance(o, dict)]
                if isinstance(outputs, list)
                else [],
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        )
    return cells


def read_notebook(path: Path) -> list[Cell]:
    return load_cells(json.loads(path.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- labels


def _find(cells: list[Cell], cell_id: str) -> Cell | None:
    """By cell id, then by nh's stable uid (ids renumbered by nbstripout), as the gateway does."""
    return next((c for c in cells if c.id == cell_id), None) or next(
        (c for c in cells if c.nh.get("uid") == cell_id), None
    )


def title_of(cells: list[Cell], cell: Cell) -> str | None:
    """The paired note's title (its first line), as ``tools.common.title_of`` reads it."""
    uid = cell.nh.get("uid") or cell.id
    note = None
    pair = cell.nh.get("pair_uid")
    if isinstance(pair, str) and pair:
        found = _find(cells, pair)
        note = found if found is not None and found.kind == "markdown" else None
    if note is None and cell.index > 0:
        prev = cells[cell.index - 1]
        if meta.is_note(prev.metadata) and prev.nh.get("pair_uid") in (uid, cell.id):
            note = prev
    if note is None or not note.source.strip():
        return None
    return normalize_title(note.source.strip().splitlines()[0]) or None


class Labels:
    """Each cell's label, built once: the note's title or the first code line, and ``[n]``."""

    def __init__(self, cells: list[Cell]) -> None:
        self.cells = cells
        self._labels: dict[int, str] = {}

    def __call__(self, cell: Cell) -> str:
        if cell.index not in self._labels:
            title = title_of(self.cells, cell)
            self._labels[cell.index] = cell_label(title, cell.count, cell.source)
        return self._labels[cell.index]

    def ref(self, cell: Cell) -> dict[str, Any]:
        return {"index": cell.index, "label": self(cell)}


def _redact(text: str) -> str:
    return secrets.current().redact(text)


def _intent(cell: Cell) -> str | None:
    value = cell.nh.get("intent")
    if not isinstance(value, str) or not value.strip():
        return None
    return _redact(" ".join(value.split()))


# ----------------------------------------------------------------------------- flag


def unreadable(source: str) -> bool:
    """nh's Python can't parse the cell once its magic lines are masked (a syntax error anywhere,
    a magic line's too: then the rules see none of the cell; newer syntax than its own; nesting too
    deep). A cell magic whose body isn't Python is left to the rules."""
    masked = mask(source)
    if masked.cell_magic is not None and masked.cell_magic not in NAMESPACE_CELL_MAGICS:
        return False
    try:
        ast.parse(masked.text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return True
    return False


def flag(project: Path, cells: list[Cell], notebook_dir: str) -> list[dict[str, Any]]:
    """The code cells that would do more than compute in the review's kernel: each is linted with
    the gateway's ``lint_cell`` and the project's config, as ``nh_add_cell`` lints a cell at that
    place. A FLAG_RULES finding flags it at any level; a rule the project set ``off`` doesn't. A
    cell nh can't parse is flagged ``unreadable`` (fail closed: the rules can't see into it)."""
    cfg = config.load(project)
    approved = hosts.read_approved(paths.Layout(project).approved_hosts)
    labels = Labels(cells)
    keys = [key for _, key in FLAG_RULES]
    found: list[dict[str, Any]] = []
    code_above: list[str] = []
    for cell in cells:
        if cell.kind != "code":
            continue
        if cell.runs:
            report = lint_cell(
                cell.source,
                title=None,
                notes=None,
                intent=None,
                cfg=cfg,
                require_note=False,
                require_intent=False,
                kernel_python=None,
                names_above=defined_names(code_above),
                approved_hosts=approved,
                code_above=code_above,
                project_root=str(project),
                notebook_dir=notebook_dir,
            )
            hit = {issue.key for issue in report.errors + report.hints + report.asks}
            rules = [key for key in keys if key in hit]
            if unreadable(cell.source):
                rules.append(UNREADABLE)
            if rules:
                title = title_of(cells, cell)
                entry = labels.ref(cell)
                entry.update(title=_redact(title) if title else None, rules=rules)
                found.append(entry)
        code_above.append(cell.source)
    return found


def flag_digest(cells: list[Cell], flagged: list[dict[str, Any]]) -> str:
    """What ``--yes`` names: the flagged cells' indexes, rules and code, hashed. A yes given for
    other cells (one added or changed since the question) doesn't match (design §6.10)."""
    sources = {cell.index: cell.source for cell in cells}
    items = [[entry["index"], entry["rules"], sources.get(entry["index"])] for entry in flagged]
    return hashlib.sha256(json.dumps(items).encode("utf-8")).hexdigest()[:DIGEST_CHARS]


# --------------------------------------------------------------------------- report


@dataclass
class Outcome:
    """One code cell's fate in the review's run."""

    status: str  # ok | error | not_run
    ename: str = ""
    evalue: str = ""
    reason: str = ""  # not_run only: NOT_RUN_REASONS


def outcomes(cells: list[Cell], run: dict, timed_out: bool) -> dict[int, Outcome]:
    """Each code cell's outcome from the parsed MARKER lines (``run["cells"]``) and, in a partial
    run, the cell that was running (``stopped``) and those after it (``timeout``). A run that isn't
    partial must have a line for every code cell: ValueError otherwise (nhctl's D151 comes first)."""
    lines = {}
    for entry in run.get("cells") or []:
        if isinstance(entry, dict) and isinstance(entry.get("i"), int):
            lines[entry["i"]] = entry
    running = run.get("running")
    result: dict[int, Outcome] = {}
    redactor = secrets.current()
    for cell in cells:
        if not cell.runs:
            continue
        entry = lines.get(cell.index)
        if entry is None:
            if not timed_out:
                raise ValueError(f"the run has no line for cell {cell.index}, and it isn't partial")
            reason = "stopped" if cell.index == running else "timeout"
            result[cell.index] = Outcome("not_run", reason=reason)
            continue
        status = entry.get("status")
        if status == "error":
            evalue = redactor.redact_head(str(entry.get("evalue") or ""), EVALUE_CHARS)
            result[cell.index] = Outcome(
                "error", redactor.redact(str(entry.get("ename") or "")), evalue[:EVALUE_CHARS]
            )
        elif status == "not_run":
            reason = entry.get("reason")
            result[cell.index] = Outcome(
                "not_run", reason=reason if reason in NOT_RUN_REASONS else "timeout"
            )
        else:
            result[cell.index] = Outcome("ok")
    return result


def read_before_defined(cells: list[Cell], labels: Labels) -> list[dict[str, Any]]:
    """Names a code cell reads when it runs (``Flow.now``: before any binding of them in the cell,
    so ``df = df.dropna()`` reads ``df``) that no code cell above it defines: each with the later
    cell that defines it (the notebook only ran because that one ran first), or None when no cell
    does (only the kernel had it). A function, lambda or method body's names (``Flow.later``: read
    when called) count only when no code cell in the notebook defines them."""
    code = [c for c in cells if c.kind == "code"]
    everywhere = defined_names([c.source for c in code])
    found: list[dict[str, Any]] = []
    above: set[str] = set()
    for position, cell in enumerate(code):
        flow = analyze(cell.source)
        judged = set(above)
        above |= defined_names([cell.source])
        if not cell.runs or not flow.parsed or flow.open or EVERYTHING in judged:
            continue
        missing = {n for n in flow.now - judged if not kernel_provides(n)}
        if EVERYTHING not in everywhere:
            missing |= {n for n in flow.later - everywhere if not kernel_provides(n)}
        if not missing:
            continue
        names = []
        for name in sorted(missing):
            later = next((c for c in code[position + 1 :] if name in analyze(c.source).bound), None)
            names.append({"name": name, "defined_in": labels(later) if later else None})
        found.append(dict(labels.ref(cell), names=names))
    return found


def out_of_order(cells: list[Cell], labels: Labels) -> list[dict[str, Any]]:
    """Code cells whose stored count is lower than a count above them (``after``: each ran before
    the cell above it with the highest count), else equal to one above them (``same_as``: one
    kernel session never gives a count twice). Cells with no count never ran and are skipped."""
    found: list[dict[str, Any]] = []
    top: Cell | None = None
    first: dict[int, Cell] = {}
    for cell in cells:
        if not cell.runs or cell.count is None:
            continue
        after = same = None
        if top is not None and top.count is not None and cell.count < top.count:
            after = dict(labels.ref(top), count=top.count)
        elif cell.count in first:
            same = dict(labels.ref(first[cell.count]), count=cell.count)
        if after or same:
            found.append(dict(labels.ref(cell), count=cell.count, after=after, same_as=same))
        first.setdefault(cell.count, cell)
        if top is None or (top.count is not None and cell.count > top.count):
            top = cell
    return found


def fails_only_fresh(
    cells: list[Cell], results: dict[int, Outcome], labels: Labels
) -> list[dict[str, Any]]:
    """Cells whose fresh run raised NameError, UnboundLocalError or KeyError while the notebook
    shows they ran (a count) without an error output. ``after_not_run``: a cell above didn't run
    in the review; ``after_error``: one failed in it. Either may be what defines the name."""
    found: list[dict[str, Any]] = []
    skipped_above = failed_above = False
    for cell in cells:
        outcome = results.get(cell.index)
        if outcome is None:
            continue
        if (
            outcome.status == "error"
            and outcome.ename in FRESH_ONLY_ERRORS
            and cell.count is not None
            and not cell.stored_error
        ):
            entry = dict(
                labels.ref(cell),
                ename=outcome.ename,
                after_not_run=skipped_above,
                after_error=failed_above,
            )
            found.append(entry)
        skipped_above = skipped_above or outcome.status == "not_run"
        failed_above = failed_above or outcome.status == "error"
    return found


def src_candidates(cells: list[Cell], labels: Labels, limit: int) -> list[dict[str, Any]]:
    """Code cells with more non-blank lines than ``[lint] max_cell_lines``, counted as L102 does."""
    found = []
    for cell in cells:
        if not cell.runs:
            continue
        count = sum(1 for line in lines_of(cell.source) if line.strip())
        if count > limit:
            found.append(dict(labels.ref(cell), lines=count))
    return found


def headings(source: str) -> list[str]:
    """The headings of a markdown cell, outside fenced code (closed only by a fence of the same
    character, at least as long): ATX lines (``## Clean``) and setext ones (a paragraph over a
    ``===`` or ``---`` line), as Jupyter renders them."""
    found: list[str] = []
    fence: tuple[str, int] | None = None
    paragraph: list[str] = []
    for line in source.splitlines():
        if fence is not None:
            close = _FENCE_CLOSE.match(line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= fence[1]:
                fence = None
            continue
        opening = _FENCE_OPEN.match(line)
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            fence, paragraph = (opening.group(1)[0], len(opening.group(1))), []
            continue
        atx = _HEADING.match(line)
        if atx or _EMPTY_HEADING.match(line):
            if atx and atx.group(1).strip():
                found.append(atx.group(1).strip())
            paragraph = []
        elif paragraph and _SETEXT.match(line):
            found.append(" ".join(part.strip() for part in paragraph))
            paragraph = []
        elif not line.strip() or _SETEXT.match(line) or _NOT_PARAGRAPH.match(line):
            paragraph = []
        elif paragraph or not line.startswith("    "):  # 4 spaces open indented code
            paragraph.append(line)
    return found


def intent_summary(cells: list[Cell], labels: Labels) -> list[dict[str, Any]]:
    """Code cells grouped under the markdown heading above them (an nh note's title is not a
    heading; a cell's last heading is the current one), each with its label and nh intent. Groups
    with no code cell are left out; the first group, before any heading, has heading None."""
    groups: list[dict[str, Any]] = [{"heading": None, "cells": []}]
    for cell in cells:
        if cell.kind == "markdown" and not meta.is_note(cell.metadata):
            groups += [{"heading": _redact(h), "cells": []} for h in headings(cell.source)]
        elif cell.runs:
            groups[-1]["cells"].append(dict(labels.ref(cell), intent=_intent(cell)))
    return [group for group in groups if group["cells"]]


def build_report(project: Path, cells: list[Cell], request: dict) -> dict[str, Any]:
    """The JSON report (design §6.10's table, without ``report`` and ``error``, which nhctl adds)."""
    cfg = config.load(project)
    limit = int(cfg["lint"]["max_cell_lines"])
    timed_out = bool(request.get("timed_out"))
    raw_run = request.get("run")
    run: dict = raw_run if isinstance(raw_run, dict) else {}
    labels = Labels(cells)
    results = outcomes(cells, run, timed_out)
    by_index = {c.index: c for c in cells}

    failing, not_run = [], []
    for index, outcome in results.items():
        ref = labels.ref(by_index[index])
        if outcome.status == "error":
            failing.append(dict(ref, ename=outcome.ename, evalue=outcome.evalue))
        elif outcome.status == "not_run":
            not_run.append(dict(ref, reason=outcome.reason))
    stopped = next((c for c in not_run if c["reason"] == "stopped"), None)
    ran = {
        status: sum(1 for o in results.values() if o.status == status)
        for status in ("ok", "error", "not_run")
    }
    complete = not timed_out
    return {
        "ok": complete and ran["ok"] == len(results),
        "complete": complete,
        "notebook": request.get("notebook"),
        "kernel": run.get("kernel"),
        "ms": request.get("ms"),
        "timeout_s": request.get("timeout_s"),
        "code_cells": len(results),
        "ran": ran,
        "failing": failing,
        "not_run": not_run,
        "stopped_at": {"index": stopped["index"], "label": stopped["label"]} if stopped else None,
        "hidden_state": {
            "read_before_defined": read_before_defined(cells, labels),
            "out_of_order": out_of_order(cells, labels),
            "fails_only_fresh": fails_only_fresh(cells, results, labels),
        },
        "src_candidates": src_candidates(cells, labels, limit),
        "max_cell_lines": limit,
        "summary": intent_summary(cells, labels),
        "flagged": request.get("flagged") or [],
    }


# ------------------------------------------------------------------------- Markdown

_REASONS = {
    "flagged": "flagged ({rules}), skipped",
    "stopped": "running when the review stopped",
    "timeout": "not reached before the review stopped",
    "kernel_died": "not run: the kernel died at an earlier cell",
}


# A fresh-only failure's note, by (a cell above failed, a cell above didn't run).
_ABOVE = {
    (True, False): " (a cell above it failed in the review)",
    (False, True): " (a cell above it didn't run in the review)",
    (True, True): " (cells above it failed or didn't run in the review)",
}


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _seconds(ms: object) -> str:
    return f"{ms / 1000:.1f} s" if isinstance(ms, (int, float)) else "?"


def render_markdown(report: dict[str, Any], when: str = "") -> str:
    """The report file: one page for a typical notebook, every section present (an empty one says
    so in one line), redacted as a whole at the end."""
    ran = report["ran"]
    counts = f"{report['code_cells']} code cells: {ran['ok']} ok, {ran['error']} failed"
    counts += f", {ran['not_run']} not run" if ran["not_run"] else ""
    head = [part for part in (when, f"kernel {report.get('kernel') or '?'}", counts) if part]
    head.append(_seconds(report.get("ms")))
    out = [f"# Review of {report.get('notebook') or 'the notebook'}", "", " · ".join(head), ""]
    if not report["complete"]:
        stopped = report.get("stopped_at")
        timeout = report.get("timeout_s")
        after = f" after {timeout:g} s" if isinstance(timeout, (int, float)) else ""
        if stopped:
            why = f"while {stopped['label']} was running, so the cells below it didn't run."
        elif ran["ok"] + ran["error"] == 0:
            why = "before any cell ran (its kernel was starting)."
        else:
            why = "between two cells, so the cells after them didn't run."
        out += [f"**Partial:** the review stopped{after} {why}", ""]

    out += ["## Failing cells", ""]
    out += [
        f"- {c['label']}: {_one_line(c['ename'] + (': ' + c['evalue'] if c['evalue'] else ''))}"
        for c in report["failing"]
    ] or ["None: every cell that ran finished without an error."]

    hidden = report["hidden_state"]
    out += ["", "## Hidden state", ""]
    if not any(hidden.values()):
        out.append("None found.")
    if hidden["read_before_defined"]:
        out.append("Names read before any cell above defines them:")
        for cell in hidden["read_before_defined"]:
            for name in cell["names"]:
                where = (
                    f"defined later in {name['defined_in']}"
                    if name["defined_in"]
                    else "which no cell defines (only the kernel had it)"
                )
                out.append(f"- {cell['label']} reads `{name['name']}`, {where}")
        out.append("")
    if hidden["out_of_order"]:
        out.append("Cells that ran out of order:")
        for c in hidden["out_of_order"]:
            if c.get("after"):
                out.append(f"- {c['label']} ran before {c['after']['label']} above it")
            else:
                out.append(
                    f"- {c['label']} has the same count as {c['same_as']['label']} above it, so "
                    "they ran in different kernel sessions"
                )
        out.append("")
    if hidden["fails_only_fresh"]:
        out.append("Cells that fail only in a fresh kernel:")
        for c in hidden["fails_only_fresh"]:
            note = _ABOVE.get((bool(c.get("after_error")), bool(c.get("after_not_run"))), "")
            out.append(
                f"- {c['label']}: {c['ename']} here, though the notebook shows it ran without "
                f"an error{note}"
            )
        out.append("")
    if out[-1] == "":
        out.pop()

    out += ["", f"## Cells over {report['max_cell_lines']} lines (candidates for src/)", ""]
    out += [f"- {c['label']}: {c['lines']} lines" for c in report["src_candidates"]] or ["None."]

    if report["not_run"]:
        rules = {f["index"]: ", ".join(f.get("rules") or []) for f in report.get("flagged") or []}
        out += ["", "## Not run", ""]
        for c in report["not_run"]:
            reason = _REASONS[c["reason"]].format(rules=rules.get(c["index"], "flagged"))
            out.append(f"- {c['label']}: {reason}")

    out += ["", "## Intent summary", ""]
    if not report["summary"]:
        out.append("No code cells.")
    for group in report["summary"]:
        if group["heading"]:
            out += [f"### {group['heading']}", ""]
        for c in group["cells"]:
            out.append(f"- {c['label']}" + (f": {c['intent']}" if c["intent"] else ""))
        out.append("")
    return _redact("\n".join(out).rstrip("\n") + "\n")


# ----------------------------------------------------------------------------- main


def answer(mode: str, request: dict) -> dict[str, Any]:
    project = Path(str(request["project"]))
    secrets.install(secrets.Redactor.for_project(project))
    cells = read_notebook(Path(str(request["copy"])))
    if mode == "flag":
        flagged = flag(project, cells, str(request.get("notebook_dir") or ""))
        return {"flagged": flagged, "digest": flag_digest(cells, flagged)}
    report = build_report(project, cells, request)
    return {"report": report, "markdown": render_markdown(report, str(request.get("when") or ""))}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in ("flag", "report"):
        sys.stderr.write("usage: python -m nh_gateway.review flag|report < request.json\n")
        return 2
    request = json.loads(sys.stdin.read())
    if not isinstance(request, dict):
        sys.stderr.write("review: the request must be a JSON object\n")
        return 2
    try:
        result = answer(args[0], request)
    except ValueError as exc:  # a run that isn't partial yet lacks a cell's line
        sys.stderr.write(f"review: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
