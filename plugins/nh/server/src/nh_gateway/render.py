"""Result text shared by the tools: a line for the human, a machine line, then '--- name ---' sections.

Cells are named by title and ``[n]`` everywhere here, never by nh- id or line number.
"""

from __future__ import annotations

import ast
import functools
import re
from collections.abc import Callable
from typing import Any, TypeVar, cast

from ._shared import secrets
from ._shared.text import clip, normalize_title

LABEL_CHARS = 40
MAX_SELF_CHECK = 8
MAX_CHECK_THIS = 5
MAX_NAMES = 5
HEADLINE_CHARS = 100
ERROR_CHARS = 120

SECTION_ORDER = (
    "check this",
    "output",
    "self-check",
    "readability hints (advisory)",
    "kernel",
    "stale",
    "config",
    "next",
)
_SECTION_ALIASES = {
    "readability hints": "readability hints (advisory)",
    "hints": "readability hints (advisory)",
}

_FILTER_CALL = re.compile(r"drop\w*|query|filter")  # method names that may drop rows
_DROP = re.compile(r"\.(?:drop\w*|query|filter)\s*\(")
_NESTED = r"(?:[^\[\]\n]|\[[^\[\]\n]*\])"  # one level of nested brackets: df[df["price"] > 0]
_BOOL_FILTER = re.compile(
    rf"\[{_NESTED}*?(?:[<>]=?|[!=]=|[~&|]"
    rf"|\.(?:isin|isna|notna|isnull|notnull|between|gt|ge|lt|le|eq|ne)\(|\.str\.\w+\()"
    rf"{_NESTED}*\]"
)
_MERGE = re.compile(r"merge\(|\.join\(")
# Calls that aggregate, reshape or take an explicit subset: fewer rows than the source is expected.
_RESHAPE = re.compile(
    r"\.(?:groupby|agg|aggregate|pivot|pivot_table|crosstab|value_counts|describe|resample|rolling"
    r"|expanding|unstack|transpose|T|size|count|nunique|unique|sum|mean|median|mode|min|max|std|var"
    r"|sem|prod|quantile|corr|cov|first|last|head|tail|nlargest|nsmallest|sample|iloc)\b"
    r"|\bcrosstab\("
)
# Self-check lines about data (frames, series, arrays): the only ones fit for a result's first line.
_DATA_LINE = re.compile(r"^[^\s:]+: (?:new )?(?:DataFrame|Series|ndarray)\b")
_DATA_RANK = {"DataFrame": 0, "Series": 1, "ndarray": 2}
_STRING = re.compile(r"'[^'\n]*'|\"[^\"\n]*\"")
_ASSIGN_LINE = re.compile(r"(?m)^[ \t]*([A-Za-z_]\w*)[ \t]*=(?!=)(.*)$")
_INPLACE_LINE = re.compile(r"(?m)^[ \t]*([A-Za-z_]\w*)\s*\..*\binplace\s*=\s*True.*$")


def cell_label(title: str | None, execution_count: int | None, source: str = "") -> str:
    """'"Drop rows with missing price" [14]'; without a title, 'the cell `df = load()` [14]' with
    the first code line (≤ 40 chars). Untitled labels start lowercase: don't open a sentence with one.
    Both are redacted before the cut (design §6.8).
    """
    redact = secrets.current().redact
    name = " ".join(redact(normalize_title(title or "")).split())
    if name:
        label = f'"{name}"'
    else:
        line = next((ln.strip() for ln in source.splitlines() if ln.strip()), "")
        line = redact(line)
        line = line if len(line) <= LABEL_CHARS else line[: LABEL_CHARS - 1].rstrip() + "…"
        label = f"the cell `{line}`" if line else "an empty cell"
    return label if execution_count is None else f"{label} [{execution_count}]"


# --- self-check -----------------------------------------------------------------------------


def selfcheck(before: dict, after: dict, *, code: str = "") -> tuple[list[str], list[str]]:
    """Diff two ``vars`` probe payloads taken before and after the run.

    Returns (self-check lines, at most 8; check-this lines). ``code`` is the cell's source; the
    merge/join and drop/filter heuristics look at it. A payload that is empty was not taken.
    Changed and new frames come first, then series and arrays, then everything else; within each
    kind, the name the cell's last line shows (or binds last) leads. See ``headline`` for the
    line a result leads with.
    """
    if not isinstance(after, dict) or not after:
        return [], []
    if "error" in after:
        return [f"self-check skipped ({_short(after['error'])})"], []
    now = _vars(after)
    if now is None:
        return [], []
    was = _vars(before) if isinstance(before, dict) else None
    lines: list[str] = []
    if was is None:
        reason = (
            _short(before["error"])
            if isinstance(before, dict) and "error" in before
            else "not taken"
        )
        lines.append(f"state before the run unknown ({reason}); after it:")
    new_is_known = was is not None and not before.get("truncated")
    rhs_by_name = _assignments(code)

    changed: list[tuple[int, str, str]] = []
    # names with no visible change, by what was compared: never "unchanged" for a frame or a
    # series, whose values were not all compared
    same: dict[str, list[str]] = {
        "same shape and nulls": [],
        "same shape": [],
        "same length": [],
        "same type": [],
        "unchanged": [],
    }
    for name, value in now.items():
        rank = _DATA_RANK.get(str(value.get("kind")), len(_DATA_RANK))
        old = was.get(name) if was is not None else None
        if old is None:
            prefix = "new " if new_is_known else ""
            rhs = rhs_by_name.get(name, [])
            origin = (
                _origin(_sources(name, rhs, now, was, code)) if _rows(value) is not None else ""
            )
            changed.append((rank, name, f"{name}: {prefix}{_describe(value, origin)}"))
            continue
        diff = _diff(old, value)
        if diff:
            changed.append((rank, name, f"{name}: {diff}"))
        else:
            same[_compared(old, value)].append(name)
    lead = _lead_name(code, {name for _, name, _ in changed})
    changed.sort(key=lambda item: (item[0], item[1] != lead))  # data first, then the cell's own
    body = [line for _, _, line in changed]

    closing: list[str] = []
    if was is not None and not after.get("truncated"):
        removed = [name for name in was if name not in now]
        if removed:
            closing.append(f"removed: {_names(removed)}")
    unchanged = [f"{what}: {_names(names)}" for what, names in same.items() if names]
    if unchanged:
        closing.append("; ".join(unchanged))
    if after.get("truncated"):
        closing.append("(more variables than the probe lists; the rest are not shown)")
    room = MAX_SELF_CHECK - len(lines) - len(closing)
    if len(body) > room:
        body = body[: room - 1] + [f"… {len(body) - room + 1} more changed or new names"]
    lines = (lines + body + closing)[:MAX_SELF_CHECK]
    return lines, _check_this(was, now, code, rhs_by_name)


def headline(selfcheck_lines: list[str]) -> str | None:
    """The self-check line a result's first line should carry, or None.

    Only a new or changed frame, series or array qualifies (``selfcheck`` lists those first).
    Constants, scalars, plots, models, 'removed' and 'same shape and nulls' lines never do: when
    None, lead with the output's first line or with nothing.
    """
    line = next((line for line in selfcheck_lines if _DATA_LINE.match(line)), None)
    return clip(line, HEADLINE_CHARS) if line else None


def _vars(payload: Any) -> dict[str, dict] | None:
    if (
        not isinstance(payload, dict)
        or "error" in payload
        or not isinstance(payload.get("vars"), dict)
    ):
        return None
    return {str(k): v for k, v in payload["vars"].items() if isinstance(v, dict)}


def _sources(
    name: str,
    rhs: list[str],
    now: dict[str, dict],
    was: dict[str, dict] | None,
    code: str = "",
) -> dict[str, dict]:
    """Frames and series the right-hand sides of ``name`` read, leftmost first.

    Each is taken as the run left it (a frame made earlier in the same cell counts too), or as
    it was before the run when the cell deleted it. A frame the cell binds again after ``name``
    (``is_priced = df["price"].notna(); df = df[is_priced]``) is taken as it was before the run,
    and left out when it didn't exist then: the run's value is not what ``name`` came from.
    """
    text = _STRING.sub('""', "\n".join(rhs))  # "select * from df" names no frame
    last_bound = _last_bound(code)
    found: list[tuple[int, str, dict]] = []
    for other, value in {**(was or {}), **now}.items():
        if last_bound.get(other, 0) > last_bound.get(name, 0):
            value = (was or {}).get(other)
            if value is None:
                continue
        if other == name or _rows(value) is None:
            continue
        match = _mention(text, other)
        if match is not None:
            found.append((match.start(), other, value))
    return {other: value for _, other, value in sorted(found, key=lambda item: item[0])}


def _origin(sources: dict[str, dict]) -> str:
    """' (from df 10,432×8)' for a self-check line; empty without a source."""
    if not sources:
        return ""
    shown = [f"{n} {_brief(v)}" for n, v in list(sources.items())[:2]]
    more = f" +{len(sources) - 2} more" if len(sources) > 2 else ""
    return f" (from {', '.join(shown)}{more})"


def _check_this(
    was: dict[str, dict] | None,
    now: dict[str, dict],
    code: str,
    rhs_by_name: dict[str, list[str]] | None = None,
) -> list[str]:
    """Surprises worth leading the reply with; see design.md §3.2 for the list."""
    notes: list[str] = []
    rhs_by_name = _assignments(code) if rhs_by_name is None else rhs_by_name
    merged = bool(_MERGE.search(code))
    known = {**(was or {}), **now}  # tells a named boolean mask from a list of column names
    for name, value in now.items():
        rows = _rows(value)
        if rows is None:
            continue
        old = (was or {}).get(name)
        old_rows = _rows(old) if old is not None and old.get("kind") == value.get("kind") else None
        rhs = rhs_by_name.get(name, [])
        existed = old is not None and old_rows is not None
        sources = {} if existed else _sources(name, rhs, now, was, code)
        if old is not None and old_rows is not None:
            if rows == 0 and old_rows > 0:
                notes.append(f"{name} now has 0 rows (was {old_rows:,}).")
            elif 0 < rows < old_rows / 2:
                pct = min(99, 100 * (old_rows - rows) // old_rows)  # never "100%" of a few left
                notes.append(f"{name} lost {pct}% of its rows ({old_rows:,} → {rows:,}).")
            elif rows > old_rows and merged:
                notes.append(
                    f"{name} grew from {old_rows:,} to {rows:,} rows after a merge/join; "
                    "duplicate keys may have multiplied rows."
                )
            elif _same_shape(old, value) and any(_filters_rows(r, known) for r in rhs):
                notes.append(
                    f"{name} still has {_extent(value)} after the drop/filter: nothing was removed."
                )
        elif was is not None and old is None and sources:
            # the frame the code starts from: leftmost on the right, the left side of a merge
            primary = next(iter(sources))
            primary_rows = _rows(sources[primary]) or 0
            twin = next((n for n, v in sources.items() if _same_shape(v, value)), None)
            filtered = any(_filters_rows(r, known) for r in rhs)
            if rows == 0 and primary_rows > 0:
                notes.append(
                    f"{name} is empty: 0 rows (from {primary}, which has {primary_rows:,})."
                )
            elif rows > primary_rows > 0 and any(_MERGE.search(r) for r in rhs):
                notes.append(
                    f"{name} has {rows:,} rows, more than {primary} ({primary_rows:,}): "
                    "the merge/join may have duplicated rows."
                )
            elif twin and filtered:
                notes.append(
                    f"{name} has the same {_extent(value)} as {twin}: the drop/filter removed nothing."
                )
            elif (
                filtered
                and 0 < rows < primary_rows / 2
                and (
                    _keeps_columns(value, sources[primary])
                    or (value.get("kind") == "Series" and any(_drop_call(r) for r in rhs))
                )
                and not any(_RESHAPE.search(_outer(r)) for r in rhs)
            ):
                pct = min(99, 100 * (primary_rows - rows) // primary_rows)
                notes.append(
                    f"{name} kept {rows:,} of the {primary_rows:,} rows in {primary} "
                    f"({pct}% removed)."
                )
        null_columns = _all_null_columns(value, old if existed else None)
        if not existed:  # a column already empty in the source is no news
            inherited = {c for v in sources.values() for c in _all_null_columns(v, None)}
            null_columns = [c for c in null_columns if c not in inherited]
        if null_columns:
            many = len(null_columns) > 1
            notes.append(
                f"{name}: {'new ' if existed else ''}column{'s' if many else ''} "
                f"{_names(null_columns)} {'are' if many else 'is'} entirely null."
            )
    return notes[:MAX_CHECK_THIS]


def _all_null_columns(value: dict, old: dict | None) -> list[str]:
    """Columns that are null in every row and did not exist before (all columns of a new frame)."""
    rows = _rows(value)
    nulls = value.get("nulls")
    if value.get("kind") != "DataFrame" or not rows or not isinstance(nulls, dict):
        return []
    before = set(_columns(old)) if old is not None else set()
    return [str(c) for c, n in nulls.items() if n == rows and str(c) not in before]


def _masked(code: str) -> str:
    """The code with IPython magic and shell lines turned into ``pass``, so ``ast`` can parse it."""
    return "\n".join(
        ln[: len(ln) - len(ln.lstrip())] + "pass" if ln.lstrip().startswith(("%", "!")) else ln
        for ln in code.splitlines()
    )


def _assignments(code: str) -> dict[str, list[str]]:
    """Name -> source of every right-hand side that (re)binds it, including ``inplace=True`` calls."""
    found: dict[str, list[str]] = {}
    masked = _masked(code)
    try:
        tree = ast.parse(masked)
    except (SyntaxError, ValueError):
        for match in _ASSIGN_LINE.finditer(code):
            found.setdefault(match.group(1), []).append(match.group(2))
        for match in _INPLACE_LINE.finditer(code):
            found.setdefault(match.group(1), []).append(match.group(0))
        return found
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            rhs = ast.get_source_segment(masked, node.value) or ""
            for target in targets:
                for name in _target_names(target):
                    found.setdefault(name, []).append(rhs)
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            inplace = any(
                kw.arg == "inplace"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in call.keywords
            )
            base = call.func.value if isinstance(call.func, ast.Attribute) else None
            if inplace and isinstance(base, ast.Name):
                found.setdefault(base.id, []).append(ast.get_source_segment(masked, call) or "")
    return found


def _last_bound(code: str) -> dict[str, int]:
    """Name -> the last line the cell binds it on (assignments only; empty if it can't parse)."""
    try:
        tree = ast.parse(_masked(code))
    except (SyntaxError, ValueError):
        return {}
    lines: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for name in (n for target in targets for n in _target_names(target)):
                lines[name] = max(lines.get(name, 0), node.lineno)
    return lines


def _target_names(target: ast.expr) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for element in target.elts for name in _target_names(element)]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    return []


def _lead_name(code: str, names: set[str]) -> str | None:
    """The name among ``names`` the cell is about: the first one its last line shows (``df``,
    ``df.shape``, ``display(df.head())``), else the last one it binds at top level."""
    try:
        body = ast.parse(_masked(code)).body
    except (SyntaxError, ValueError):
        return None
    if body and isinstance(body[-1], ast.Expr):
        shown = [n for n in ast.walk(body[-1]) if isinstance(n, ast.Name) and n.id in names]
        if shown:
            return min(shown, key=lambda n: (n.lineno, n.col_offset)).id
    for node in reversed(body):
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound = [name for target in targets for name in _target_names(target) if name in names]
            if bound:
                return bound[-1]
    return None


def _drops_or_filters(rhs: str) -> bool:
    return bool(_DROP.search(rhs) or _BOOL_FILTER.search(rhs))


def _filters_rows(rhs: str, known: dict[str, dict]) -> bool:
    """Whether a right-hand side may drop rows: a ``.drop*``, ``.query`` or ``.filter`` call, or
    indexing by a mask (``df[df.price > 0]``, ``df.loc[~bad]``, ``df[df.price.gt(0)]``, or a
    name ``known`` lists as a Series or array, such as ``df[is_expensive]``).

    Indexing by column names or a slice does not count. Text ``ast`` can't parse falls back to
    the regexes.
    """
    try:
        tree = ast.parse(f"({rhs}\n)", mode="eval")
    except (SyntaxError, ValueError):
        return _drops_or_filters(rhs)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if _FILTER_CALL.fullmatch(node.func.attr):
                return True
        elif isinstance(node, ast.Subscript) and _is_mask(node.slice, known):
            return True
    return False


def _drop_call(rhs: str) -> bool:
    """Whether a right-hand side calls ``.drop*``, ``.query`` or ``.filter``: rows dropped on
    purpose, even from a single column (``df["price"].dropna()``)."""
    try:
        tree = ast.parse(f"({rhs}\n)", mode="eval")
    except (SyntaxError, ValueError):
        return bool(_DROP.search(rhs))
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and bool(_FILTER_CALL.fullmatch(node.func.attr))
        for node in ast.walk(tree)
    )


def _is_mask(key: ast.expr, known: dict[str, dict]) -> bool:
    """Whether a subscript's key selects rows by a condition (for ``.loc[rows, cols]``, the rows)."""
    if isinstance(key, ast.Tuple) and key.elts:
        key = key.elts[0]
    if isinstance(key, ast.Name):
        return (known.get(key.id) or {}).get("kind") in ("Series", "ndarray")
    if isinstance(key, ast.UnaryOp):
        return isinstance(key.op, ast.Invert)
    if isinstance(key, ast.BinOp):
        return isinstance(key.op, (ast.BitAnd, ast.BitOr, ast.BitXor))
    return isinstance(key, (ast.Compare, ast.BoolOp, ast.Call, ast.Lambda))


def _keeps_columns(value: dict, source: dict) -> bool:
    """Whether ``value`` is a row subset of ``source`` rather than a pick from it: the same kind
    and, for a frame, every column of the source. A column or a few columns taken out for a
    look (``df.loc[bad, "date"]``) are not rows lost."""
    if value.get("kind") != source.get("kind"):
        return False
    if value.get("kind") != "DataFrame":
        return True
    kept, wanted = _complete_columns(value), _complete_columns(source)
    if kept is not None and wanted is not None:
        return set(wanted) <= set(kept)
    shape, source_shape = _shape(value) or [0, 0], _shape(source) or [0, 0]
    return shape[-1] >= source_shape[-1]


def _mention(text: str, name: str) -> re.Match[str] | None:
    return re.search(rf"(?<![\w.]){re.escape(name)}\b", text)


def _outer(rhs: str) -> str:
    """The call chain with every bracket's and parenthesis' contents dropped.

    ``df[df.price > df.price.mean()]`` -> ``df[]``: a reduction inside the filter's mask is not
    an aggregation of the result.
    """
    for _ in range(50):
        shorter = re.sub(r"\[[^\[\]]*\]", "[]", re.sub(r"\([^()]*\)", "()", rhs))
        if shorter == rhs:
            break
        rhs = shorter
    return rhs


# --- variable summaries ---------------------------------------------------------------------


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _shape(value: dict) -> list[int] | None:
    shape = value.get("shape")
    if isinstance(shape, (list, tuple)) and all(_int(n) is not None for n in shape):
        return list(shape)
    return None


def _rows(value: dict | None) -> int | None:
    if not isinstance(value, dict):
        return None
    if value.get("kind") == "DataFrame":
        shape = _shape(value)
        return shape[0] if shape else None
    if value.get("kind") == "Series":
        return _int(value.get("len"))
    return None


def _columns(value: dict | None) -> list[str]:
    columns = value.get("columns") if isinstance(value, dict) else None
    return [str(c) for c in columns] if isinstance(columns, list) else []


def _complete_columns(value: dict) -> list[str] | None:
    """The column list, when the probe listed every column (wide frames may be cut)."""
    shape, columns = _shape(value), _columns(value)
    return columns if shape and len(shape) == 2 and len(columns) == shape[1] else None


def _same_shape(old: dict, new: dict) -> bool:
    if old.get("kind") != new.get("kind"):
        return False
    if new.get("kind") == "DataFrame":
        return _shape(old) is not None and _shape(old) == _shape(new)
    return _rows(old) is not None and _rows(old) == _rows(new)


def _size(value: dict) -> str:
    """'10,432×8' for frames, '10,432 values' for series."""
    shape = _shape(value)
    if value.get("kind") == "DataFrame" and shape and len(shape) == 2:
        return f"{shape[0]:,}×{shape[1]:,}"
    rows = _rows(value)
    return f"{rows:,} values" if rows is not None else "?"


def _extent(value: dict) -> str:
    """'10,432 rows × 8 columns' for frames, '10,432 values' for series."""
    shape = _shape(value)
    if value.get("kind") == "DataFrame" and shape and len(shape) == 2:
        return f"{shape[0]:,} rows × {shape[1]:,} columns"
    return f"{_rows(value) or 0:,} values"


def _kind(value: dict) -> str:
    kind = str(value.get("kind") or "?")
    lib = value.get("lib")
    return (
        f"{kind} ({lib})" if lib and lib != "pandas" and kind in ("DataFrame", "Series") else kind
    )


def _short_type(value: dict) -> str:
    return str(value.get("type") or "?").rsplit(".", 1)[-1]


def _nulls_text(value: dict) -> str:
    nulls = value.get("nulls")
    if isinstance(nulls, dict):
        present = [f"{c} {n:,}" for c, n in nulls.items() if _int(n)]
        return f"; nulls: {_names(present, 3)}" if present else "; no nulls"
    count = _int(nulls)
    return f"; nulls {count:,}" if count else ""


def _brief(value: dict) -> str:
    """'10,432×8' for frames, 'len 10,432' for series."""
    return _size(value) if value.get("kind") == "DataFrame" else f"len {_rows(value) or 0:,}"


_Summary = TypeVar("_Summary", bound=Callable[..., Any])


def _redacted(func: _Summary) -> _Summary:
    """A summary line with its names and values redacted (design §6.8); scalar reprs are
    already redacted before ``_short`` cuts them."""

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> str | None:
        text = func(*args, **kwargs)
        return secrets.current().redact(text) if text else text

    return cast(_Summary, wrapper)


@_redacted
def _describe(value: dict, origin: str = "") -> str:
    """One name's summary; ``origin`` (' (from df 10×2)') follows the size of a frame or series."""
    kind = value.get("kind")
    if kind == "DataFrame":
        return f"{_kind(value)} {_size(value)}{origin}{_nulls_text(value)}"
    if kind == "Series":
        rows = _rows(value)
        length = f"len {rows:,}" if rows is not None else "len ?"
        head = f"{_kind(value)} {length} {value.get('dtype') or ''}".rstrip()
        return head + origin + _nulls_text(value)
    if kind == "ndarray":
        shape = _shape(value)
        return f"ndarray {tuple(shape) if shape else '?'} {value.get('dtype') or ''}".rstrip()
    if kind == "scalar":
        return f"{value.get('type') or '?'} {_short(value.get('repr', ''))}".rstrip()
    if kind == "container":
        length, name = _int(value.get("len")), str(value.get("type") or "?")
        return f"{name} len {length:,}" if length is not None else name
    if kind == "object":
        return _short_type(value)
    return str(kind or "?")


@_redacted
def _diff(old: dict, new: dict) -> str | None:
    """What changed between two summaries of one name; None when nothing visible did."""
    kind = new.get("kind")
    if old.get("kind") != kind:
        return f"{_describe(old)} → {_describe(new)}"
    if kind == "DataFrame":
        return _diff_frame(old, new)
    if kind == "Series":
        parts = []
        before, after = _rows(old), _rows(new)
        if before != after and before is not None and after is not None:
            parts.append(f"len {before:,} → {after:,} ({after - before:+,})")
        if old.get("dtype") != new.get("dtype"):
            parts.append(f"dtype {old.get('dtype')} → {new.get('dtype')}")
        old_nulls, new_nulls = _int(old.get("nulls")), _int(new.get("nulls"))
        if old_nulls is not None and new_nulls is not None and old_nulls != new_nulls:
            parts.append(f"nulls {old_nulls:,} → {new_nulls:,}")
        if not parts and _values_changed(old, new):
            return f"Series {_brief(new)}; values changed"
        return f"Series {'; '.join(parts)}" if parts else None
    if kind == "ndarray":
        if _shape(old) == _shape(new) and old.get("dtype") == new.get("dtype"):
            return None
        return f"{_describe(old)} → {_describe(new)}"
    if kind == "scalar":
        if old.get("repr") == new.get("repr") and old.get("type") == new.get("type"):
            return None
        return f"{_short(old.get('repr', ''))} → {_short(new.get('repr', ''))}"
    if kind == "container":
        if old.get("type") == new.get("type") and old.get("len") == new.get("len"):
            return None
        return f"{_describe(old)} → {_describe(new)}"
    if old.get("type") != new.get("type"):
        return f"{_short_type(old)} → {_short_type(new)}"
    return None


def _diff_frame(old: dict, new: dict) -> str | None:
    parts: list[str] = []
    old_shape, new_shape = _shape(old), _shape(new)
    head = f"{_kind(new)} {_size(new)}"
    if old_shape != new_shape:
        head = f"{_kind(new)} {_size(old)} → {_size(new)}"
        if old_shape and new_shape and old_shape[0] != new_shape[0]:
            head += f" ({new_shape[0] - old_shape[0]:+,} rows)"
    old_cols, new_cols = _complete_columns(old), _complete_columns(new)
    if old_cols is not None and new_cols is not None:
        added = [c for c in new_cols if c not in set(old_cols)]
        dropped = [c for c in old_cols if c not in set(new_cols)]
        if added:
            parts.append(f"new columns {_names(added)}")
        if dropped:
            parts.append(f"dropped columns {_names(dropped)}")
    old_types, new_types = old.get("dtypes"), new.get("dtypes")
    if isinstance(old_types, dict) and isinstance(new_types, dict):
        retyped = [
            f"{c} {old_types[c]} → {t}"
            for c, t in new_types.items()
            if c in old_types and old_types[c] != t
        ]
        if retyped:
            parts.append(f"dtype {_names(retyped, 2)}")
    old_nulls, new_nulls = old.get("nulls"), new.get("nulls")
    if isinstance(old_nulls, dict) and isinstance(new_nulls, dict):
        present = set(_columns(new)) or None
        moved = []
        for column in dict.fromkeys([*old_nulls, *new_nulls]):
            if present is not None and str(column) not in present:
                continue
            a, b = _int(old_nulls.get(column)) or 0, _int(new_nulls.get(column)) or 0
            if a != b:
                moved.append(f"{column} {a:,} → {b:,}")
        if moved:
            parts.append(f"nulls {_names(moved, 3)}")
    if old_shape == new_shape and not parts:
        return f"{head}; values changed" if _values_changed(old, new) else None
    return "; ".join([head, *parts])


def _values_changed(old: dict, new: dict) -> bool:
    """Whether the probe's value fingerprints (``sums``: numeric column sums) differ."""
    before, after = old.get("sums"), new.get("sums")
    return before is not None and after is not None and before != after


def _compared(old: dict, value: dict) -> str:
    """What the self-check compared for a name that shows no change, as its closing-line label.

    Only a scalar's value is compared in full; a frame's or series' values are not (at most
    their numeric sums), so they are never called "unchanged". Nulls are named only when the
    probe counted them both times (it skips frames over ``max_cells``).
    """
    kind = value.get("kind")
    if kind == "scalar":
        return "unchanged"
    if kind == "container":
        return "same length"
    if kind in ("DataFrame", "Series", "ndarray"):
        counted = "nulls" in old and "nulls" in value
        return "same shape and nulls" if counted else "same shape"
    return "same type"


def _names(items: list[str], limit: int = MAX_NAMES) -> str:
    shown = ", ".join(items[:limit])
    return f"{shown} +{len(items) - limit} more" if len(items) > limit else shown


def _short(value: Any, limit: int = 40) -> str:
    text = " ".join(secrets.current().redact(str(value)).split())  # redacted before the cut
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- next block and result ------------------------------------------------------------------


def _plural(count: int, word: str, plural: str) -> str:
    return f"{count} {word if count == 1 else plural}"


def next_block(
    status: str,
    *,
    retries_left: int,
    waits_left: int,
    cell: str,
    audience: str = "main",
    batch: tuple[int, int, int] | None = None,
    check_this: bool = False,
) -> str:
    """The reply contract for the agent, by run status (plan §4.2).

    ``audience="writer"``: nh:cell-writer's texts. Its final answer goes to the nh:qa-cell
    workflow, never to the user, so each says to return the whole result there.

    ``batch``: (step, total, stopped_at) for a cell of the message's approved batch (design
    §6.3; ``stopped_at`` 0 while it goes), with ``check_this`` when the result has that section.
    """
    if audience == "writer":
        plain = _writer_next(status, retries_left=retries_left, waits_left=waits_left, cell=cell)
        if batch is None or not batch[2]:  # a going batch: each step's run returns its result
            return plain
        return _writer_batch_next(status, plain, cell=cell, batch=batch, check_this=check_this)
    plain = _main_next(status, retries_left=retries_left, waits_left=waits_left, cell=cell)
    if batch is None:
        return plain
    return _batch_next(status, plain, cell=cell, batch=batch, check_this=check_this)


BATCH_LAST = "That was step {total} of {total}, the last of the approved batch. "
NO_RETRY = (
    "Don't fix it in this message: a batch has no retries. Explain in plain words: quote the "
    "failing code, what Python said, the likely cause and one fix; "
)


def _batch_next(
    status: str, plain: str, *, cell: str, batch: tuple[int, int, int], check_this: bool
) -> str:
    """The next block of a cell in an approved batch (design §6.3): go on to the next step,
    or stop there and report. At the batch's last step every planned step ran, so a stop there
    doesn't ask which did not."""
    step, total, stop = batch
    subject = cell[:1].upper() + cell[1:]
    if not stop:
        if status != "ok":  # a result that isn't ok always stops the batch
            return plain
        if step < total:
            return (
                f"Step {step} of {total} of the approved batch ran OK. Give the user a short "
                f"report on {cell}: what it did and the real numbers, surprises first, named by "
                f"title and [n]. Then write step {step + 1} of the plan with nh_add_cell, "
                "without waiting for the user."
            )
        last = plain.replace("Do not write a second cell.", "Do not write another cell.")
        return BATCH_LAST.format(total=total) + last
    here = stop == step
    rest = not here or step < total  # some planned step did not run
    if status == "ok" and check_this and here:
        unrun = ", and say which planned steps did not run" if rest else ""
        return (
            f"The approved batch stops at step {step} of {total}: {cell} ran, but its result "
            "needs a look (see 'check this'). Reply to the user about it: lead with the 'check "
            f"this' finding, then what the cell did and the real numbers{unrun}. Write no other "
            "cell and don't change this one in this message; wait for the user."
        )
    if status == "ok" and check_this:
        return (
            f"{subject} ran, but its result needs a look (see 'check this'), and the approved "
            f"batch stopped at step {stop} of {total}. Reply to the user: lead with the 'check "
            "this' finding, then what each step did and where and why the batch stopped. Write "
            "no other cell and don't change this one in this message; wait for the user."
        )
    if status == "ok":
        return (
            f"{subject} ran OK, but the approved batch stopped at step {stop} of {total}. Report "
            "the batch to the user: what each step did, then where and why it stopped. Write no "
            "other cell; wait for the user."
        )
    if status == "error" and here:
        unrun = "say which planned steps did not run; " if rest else ""
        return (
            f"The approved batch stops at step {step} of {total}: {subject} failed. "
            f"{NO_RETRY}{unrun}then wait."
        )
    if status == "error":
        return (
            f"{subject} failed, and the approved batch stopped at step {stop} of {total}. "
            f"{NO_RETRY}say where and why the batch stopped and which planned steps did not run; "
            "then wait."
        )
    verb = "stops" if here else "stopped"
    unrun = ": say which planned steps did not run" if rest else ""
    return f"{plain} The approved batch {verb} at step {stop} of {total}{unrun}."


def _writer_batch_next(
    status: str, plain: str, *, cell: str, batch: tuple[int, int, int], check_this: bool
) -> str:
    """nh:cell-writer's next block once the approved batch has stopped (design §6.3): nothing
    more changes this message, so no retry and no revision; the result goes to the workflow."""
    step, total, stop = batch
    subject = cell[:1].upper() + cell[1:]
    back = f"{_TO_WORKFLOW[:1].upper()}{_TO_WORKFLOW[1:]} (status {status})"
    here = stop == step
    if status == "error":
        how = (
            f"so the approved batch stops here, at step {step} of {total}"
            if here
            else f"and the approved batch stopped at step {stop} of {total}"
        )
        return f"{subject} failed, {how}: a batch has no retries. {back}. Don't change it again."
    if status == "ok":
        if check_this and here:
            why = (
                f"{subject} ran, but its result needs a look (see 'check this'), so the approved "
                f"batch stops here, at step {step} of {total}"
            )
        else:
            why = f"{subject} ran OK, but the approved batch stopped at step {stop} of {total}"
        return (
            f"{why}: no revision of it this message. {back}. Don't reply to the user or write a "
            "second cell."
        )
    verb = "stops" if here else "stopped"
    return f"{plain} The approved batch {verb} at step {stop} of {total}."


def _main_next(status: str, *, retries_left: int, waits_left: int, cell: str) -> str:
    """The main conversation's reply contract for one cell's result."""
    subject = cell[:1].upper() + cell[1:]  # opens a sentence; untitled labels read "the cell `…`"
    if status == "ok":
        return (
            f"Reply to the user about {cell}: (1) what the cell does; (2) why this approach; "
            "(3) judgment calls they may want to change; (4) the real numbers from the output, "
            "surprises first (see 'check this'); (5) any failed attempts; (6) one proposed next cell, "
            'as a title they can approve with "go". Name cells by title and [n]. '
            "Do not write a second cell."
        )
    if status == "error" and retries_left > 0:
        return (
            f"{subject} failed. Fix it with nh_edit_cell on the same cell "
            f"({_plural(retries_left, 'retry', 'retries')} left this message), then tell the user "
            "what failed and what you changed."
        )
    if status == "error":
        return (
            f"{subject} failed and no retries are left. Explain in plain words: quote the failing code, "
            "what Python said, the likely cause and one fix. Offer to undo the cell. Then wait."
        )
    if status in ("running", "queued"):
        waits = _plural(waits_left, "wait", "waits")
        wait = (
            f'You may call nh_run(mode="wait") on it ({waits} left); then stop.'
            if waits_left > 0
            else "No waits are left: stop here."
        )
        if status == "queued":
            return (
                f"{subject} has not started: it waits behind a cell already running in the kernel, "
                "so nothing of it has run yet. Tell the user; they can wait, or stop the running "
                f"cell in JupyterLab. {wait} Do not write another cell."
            )
        return (
            f"{subject} is still running; its outputs keep appearing in JupyterLab. Tell the user. "
            f"{wait} Do not write another cell."
        )
    if status == "aborted":
        return (
            f"The cell ahead of yours failed; nothing of yours ran, so {cell} has no result. "
            "This used no retry. Tell the user, and offer to run it once the earlier cell is fixed. "
            "Then wait."
        )
    if status == "timeout":
        return (
            f"{subject} did not finish within the time limit ([exec] hard_timeout_s in harness.toml). "
            "Tell the user what finished (see the output), and suggest a faster approach such as a "
            "sample, or a longer limit. Do not write another cell. Then wait."
        )
    if status == "interrupted":
        return (
            f"{subject} was stopped before it finished (interrupted); the output above is what ran "
            "before that. This is not a failure to fix: the user may have stopped it on purpose. "
            "Tell the user what finished and what did not. Do not re-run or edit it without "
            "asking first. Then wait."
        )
    if status == "deleted":
        return (
            f"The user deleted {cell} in JupyterLab while it ran; take that as a no. Do not "
            "rebuild, re-add or re-run it. Tell the user what ran before they removed it (its "
            "variables may still be in the kernel) and ask what they want instead. Then wait."
        )
    if status == "lost":
        return (
            f"The kernel restarted, died or disconnected while {cell} ran, so there is no result "
            "and earlier variables may be gone. Tell the user; they can rebuild them with Kernel → "
            "Restart Kernel and Run Up to Selected Cell in JupyterLab. Do not write another cell. "
            "Then wait."
        )
    return f"Tell the user what happened to {cell}, then wait."


_TO_WORKFLOW = "return this whole result to the workflow as your final answer"


def _writer_next(status: str, *, retries_left: int, waits_left: int, cell: str) -> str:
    """nh:cell-writer's next block: fix or wait only where the main conversation would too."""
    subject = cell[:1].upper() + cell[1:]
    back = f"{_TO_WORKFLOW} (status {status})"
    stop = back[:1].upper() + back[1:]
    if status == "ok":
        return f"{subject} ran OK. {stop}. Don't reply to the user or write a second cell."
    if status == "error" and retries_left > 0:
        return (
            f"{subject} failed. Fix it with nh_edit_cell on the same cell "
            f"({_plural(retries_left, 'retry', 'retries')} left this message) and return the new "
            f"result instead; if you can't, {back}."
        )
    if status == "error":
        return f"{subject} failed and no retries are left. {stop}. Don't change it again."
    if status in ("running", "queued"):
        waits = _plural(waits_left, "wait", "waits")
        wait = (
            f'You may call nh_run(mode="wait") on it ({waits} left) and return that result '
            f"instead; otherwise {back}."
            if waits_left > 0
            else f"No waits are left: {back}."
        )
        if status == "queued":
            return (
                f"{subject} has not started: it waits behind a cell already running in the "
                f"kernel. {wait} Do not write another cell."
            )
        return f"{subject} is still running. {wait} Do not write another cell."
    if status == "aborted":
        return f"The cell ahead of yours failed, so nothing of {cell} ran. {stop}; don't re-run it."
    if status == "timeout":
        return f"{subject} did not finish within the time limit. {stop}; don't change it again."
    if status == "interrupted":
        return (
            f"{subject} was stopped before it finished; the user may have stopped it on purpose. "
            f"{stop}; don't change or re-run it."
        )
    if status == "deleted":
        return (
            f"The user deleted {cell} in JupyterLab while it ran; take that as a no. {stop}; "
            "don't rebuild, re-add or re-run it."
        )
    if status == "lost":
        return (
            f"The kernel restarted, died or disconnected while {cell} ran, so there is no result. "
            f"{stop}; don't write another cell."
        )
    return f"{subject} ended {status}. {stop}; don't change it again."


def error_summary(ename: str, evalue: str, limit: int = ERROR_CHARS) -> str:
    """'ValueError: day is out of range for month…': one line, cut at a word boundary.

    Only the message's first line is kept; '…' marks that something was cut, so callers should
    not add a full stop after it.
    """
    lines = [line.strip() for line in (evalue or "").splitlines() if line.strip()]
    text = f"{ename}: {lines[0]}" if lines else (ename or "Error")
    text = secrets.current().redact_head(text, limit)  # before the cut (design §6.8)
    if len(text) > limit:
        return clip(text, limit)
    return text.rstrip(" ,;:-") + "…" if len(lines) > 1 else text


class Result:
    """A tool result: human line, machine line, then sections in plan order ('next' last).

    ``lead`` lines go above the first line, for news the human must see before anything else
    ("Kernel ≠ notebook: …", "NEW kernel: …").
    """

    def __init__(self, first_line: str, machine: str) -> None:
        self.first_line = first_line
        self.machine = machine
        self._lead: list[str] = []
        self._sections: list[tuple[str, str]] = []

    def lead(self, lines: list[str] | str) -> Result:
        """Put lines above the first line, in call order; empty ones are skipped."""
        for line in [lines] if isinstance(lines, str) else lines:
            if line and line.strip():
                self._lead.append(line.rstrip("\n"))
        return self

    def section(self, name: str, lines: list[str] | str) -> Result:
        """Add a section; empty ones are skipped. Returns self for chaining."""
        body = lines if isinstance(lines, str) else "\n".join(line for line in lines if line)
        if body.strip():
            self._sections.append((_SECTION_ALIASES.get(name, name), body.rstrip("\n")))
        return self

    def text(self) -> str:
        def rank(item: tuple[str, str]) -> float:
            name = item[0]
            return SECTION_ORDER.index(name) if name in SECTION_ORDER else len(SECTION_ORDER) - 1.5

        parts = [*self._lead, self.first_line, self.machine]
        for name, body in sorted(self._sections, key=rank):
            parts.append(f"--- {name} ---\n{body}")
        return "\n".join(parts)
