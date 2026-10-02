"""Pre-write lint (FR-3): hard rules reject a cell before anything is written; hints ride along.

Messages quote the code and never cite line numbers: JupyterLab hides them by default.
"""

from __future__ import annotations

import ast
import builtins
import functools
import io
import re
import sys
import textwrap
import tokenize
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal

from nh_gateway._shared.patterns import CELL_NOTEBOOK_WRITES, CELL_PACKAGE_INSTALL, CELL_SEPARATORS
from nh_gateway._shared.text import count_words, normalize_bullet, normalize_title, split_notes
from nh_gateway.config import ASK_RULES, Config
from nh_gateway.dataflow import EVERYTHING, Flow, base_name, flow_of
from nh_gateway.lint import secret_scan
from nh_gateway.lint.magics import Masked, lines_of, mask

Severity = Literal["error", "hint", "ask"]
# (message, fix), or (message, fix, question) for a rule that can be an ask (design §6.4)
Finding = tuple[str, str] | tuple[str, str, str]

# Cell magics whose body is Python source (ruff skips the same set).
PYTHON_CELL_MAGICS = frozenset({"time", "timeit", "capture", "prun", "debug", "python", "python3"})
_SHELL_CELL_MAGICS = frozenset({"bash", "sh", "script", "system", "!"})
_PROSE_CALLS = frozenset({"Markdown", "HTML", "Latex", "display_markdown", "display_html"})
_NOTE_HINTS = frozenset({"L122", "L123"})
_SEPARATOR_FIX = (
    "Keep only the first step in this cell (no separators) and propose the rest in your reply."
)
_MAX_INTENT_CHARS = 200
_MAX_PRINT_WORDS = 15

_PRAGMA = re.compile(r"#\s*(?:noqa|type:|pragma|fmt:|pyright:|pylint:|mypy:|isort:|ruff:)", re.I)
_HTML = re.compile(
    r"<!--|</?(?:a|abbr|b|blockquote|br|center|code|details|div|em|font|h[1-6]|hr|i|iframe|img"
    r"|kbd|li|mark|ol|p|pre|s|script|small|span|strong|style|sub|summary|sup|table|tbody|td|th"
    r"|thead|tr|u|ul)\b[^>]*>",
    re.I,
)
_TITLE_MARKUP = re.compile(r"`|!\[|\*\*|__|\[[^\]]*\]\(|#(?:\s|#|$)")
_HEADING = re.compile(r"^#{1,6}(?:\s|$)")
_FENCE = re.compile(r"```|~~~")
_NESTED_ITEM = re.compile(r"^\s+(?:[-*•+]|\d+[.)])\s+")
_LIST_MARKER = re.compile(r"^(?:[-*•+]|\d+[.)])\s+")
# Installs the shared pattern misses: `!{sys.executable} -m pip install`, shell cells, os.system.
_MAGIC_INSTALL = re.compile(
    r"^\s*[!%].*?(?:-m\s+pip|\b(?:pip3?|conda|mamba|micromamba|uv))\s+(?:install|add)\b",
    re.I | re.M,
)
_SHELL_INSTALL = re.compile(
    r"^\s*(?:sudo\s+)?(?:python3?\s+-m\s+pip|pip3?|conda|mamba|micromamba|uv(?:\s+pip)?)"
    r"\s+(?:install|add)\b",
    re.I | re.M,
)
_OS_INSTALL = re.compile(
    r"\bos\.(?:system|popen)\([^)]*\b(?:pip3?|conda|mamba|uv)\b[^)]*\b(?:install|add)\b", re.I
)
# L009's text (design §6.4). A line that may hold a package command (a magic, a shell call from
# Python, or a shell cell's command), where a line splits into commands, and a command: a tool
# word, then its verb, with no other tool word between them (so the scan stays linear).
_COMMAND_LINE = re.compile(r"^\s*[!%]|\b(?:os\.(?:system|popen)|subprocess\.[a-z_]+)\(")
_SHELL_PACKAGE_LINE = re.compile(
    r"^\s*(?:sudo\s+)?(?:python3?\s+-m\s+pip|pip3?|conda|mamba|micromamba|uv(?:\s+pip)?)"
    r"\s+(?:install|add|uninstall|remove)\b",
    re.I,
)
_COMMAND_SPLIT = re.compile(r"&&|\|\||;|(?<=\s)\|")
_TOOL = r"\b(?:pip3?|conda|mamba|micromamba|uv)\b"
_COMMAND = re.compile(
    rf"(?P<tool>{_TOOL})(?P<mid>(?:(?!{_TOOL}).)*?)(?<![\w-])"
    r"(?P<verb>uninstall|install|remove|add)\b",
    re.I,
)
_CONDA_TOOLS = frozenset({"conda", "mamba", "micromamba"})
_COMMAND_END = re.compile(r"\)|[\'\"]\]|[\'\"]\s*,\s*(?![\'\"\s])|\s\d*>|\s#")
_INSTALL_WORD = re.compile(r"[\w.\-+=<>!~@:/${}*%,#&?]+(?:\[[\w,.\-]*\])?")
# The options whose value L009 names (design §6.4); the values of the others in _VALUE_OPTION
# (pip's, uv's and conda's) are left out, so they aren't read as packages.
_REQUIREMENT_OPTION = re.compile(r"-r|--requirements?|--file")
_EDITABLE_OPTION = re.compile(r"-e|--editable")
_INDEX_OPTION = re.compile(r"-[if]|--(?:index-url|extra-index-url|index|default-index|find-links)")
_CHANNEL_OPTION = re.compile(r"-c|--channel")
_VALUE_OPTION = re.compile(
    r"-[cefinprt]|--(?:requirements?|file|constraint|editable|index-url|extra-index-url"
    r"|find-links|target|prefix|root|python|name|channel|index|default-index|group|optional"
    r"|extra|package|src|trusted-host|platform|python-version|upgrade-strategy|only-binary"
    r"|no-binary|log|cache-dir|timeout|retries|proxy|cert|client-cert)"
)
_SHELL_SAFE = re.compile(r"[\w@%+=:,./-]+")
_PROSE_CALL_TEXT = re.compile(
    r"\b(?:Markdown|HTML|Latex)\s*\(\s*[rRbBuUfF]{0,2}(\"\"\"|'''|\"|')(.*?)\1", re.S
)
_STAR_IMPORT = re.compile(r"^\s*from\s+(\S+)\s+import\s+\*", re.M)
_BARE_EXCEPT = re.compile(r"^\s*except\s*:", re.M)
_MAGIC_NAME = re.compile(r"^\s*%{1,2}(\w+)")
_LINE_REF = re.compile(r"\s*\(?\b(?:on|at) line \d+\)?")  # "... after 'for' statement on line 1"
_ASSIGNED_MAGIC = re.compile(r"^\s*[A-Za-z_][\w\s,]*=\s*[!%]")
_SILENT_MAGIC = re.compile(
    r"matplotlib|(?:re)?load_ext|autoreload|aimport|config|env|precision|xmode|colors"
    r"|(?:un)?alias|alias_magic"
)
_CRYPTIC = re.compile(
    r"(?:df|data|tmp|temp|res|out|new|val|arr|x)_?\d+|tmp|temp|foo|bar|baz|dummy|stuff|thing|blah",
    re.I,
)
# Names the kernel provides without any cell defining them: builtins plus IPython's.
_BUILTINS = frozenset(dir(builtins))
_IPYTHON_NAME = re.compile(
    r"In|Out|get_ipython|display|exit|quit|_{1,3}|_i{1,3}|_[iod]h|__builtins?__|_i?\d+"
)

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_COMPS = (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)
_PRINTS = frozenset({"print", "display", "pprint", "pp"})
_PLT_FIGURE = frozenset({"figure", "subplots", "subplot_mosaic"})
_PLT_DRAW = re.compile(
    r"plot|barh?|scatter|hist2?d?|boxplot|violinplot|pie|imshow|matshow|errorbar|fill_between"
    r"|stem|step|stackplot|hexbin|contourf?|pcolormesh|ax[hv]line|loglog|semilog[xy]"
)
_SNS_FIGURE = re.compile(r"relplot|displot|catplot|pairplot|jointplot|lmplot|clustermap")
_SNS_SETUP = re.compile(
    r"set|set_theme|set_style|set_palette|set_context|color_palette|despine|axes_style"
    r"|plotting_context"
)
_FRAME_PLOTS = frozenset({"plot", "hist", "boxplot"})
# Calls that draw a figure of their own: sklearn's `RocCurveDisplay.from_estimator`,
# `plot_tree`, shap's `summary_plot`, pandas' `scatter_matrix`.
_FIGURE_CALL = re.compile(r"from_estimator|from_predictions|plot_\w+|\w+_plot|scatter_matrix")
_APPENDS = frozenset({"append", "extend", "insert", "pop", "remove", "popitem"})
_NOT_CODE = frozenset({tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE}) | {
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENDMARKER,
}


@dataclass
class Issue:
    rule: str  # "L004"
    key: str  # "notes" (config key for hints; hard rules use their own key)
    severity: Severity
    message: str  # human sentence, quotes code, never line numbers
    fix: str  # one sentence telling the agent what to change
    question: str = ""  # an ask's clause for the user ("installs `x` into the kernel only")


@dataclass
class LintReport:
    errors: list[Issue]
    hints: list[Issue]
    asks: list[Issue]  # what holds the cell for the user's yes (E122, design §6.4)
    code_lines: int
    comment_lines: int
    defs: set[str]
    uses: set[str]
    parsed: bool
    title: str | None
    bullets: list[str]  # normalised values to write

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True)
class _Comment:
    line: int  # 1-based
    col: int
    text: str
    alone: bool  # nothing but indentation before it


@dataclass
class _Cell:
    """Everything the rules look at, computed once."""

    cfg: Config
    lines: list[str]  # raw, newline-normalised
    masked: Masked
    tree: ast.Module | None  # None when unparsable or a %% cell
    parse_error: Exception | None
    flow: Flow
    comments: list[_Comment]
    code_lines: set[int]
    comment_lines: set[int]
    code_text: str  # raw source with comments cut off
    title: str | None
    intent: str | None
    bullets: list[str]
    names_above: set[str] | None

    @classmethod
    def build(
        cls,
        code: str,
        cfg: Config,
        *,
        title: str | None,
        intent: str | None,
        bullets: list[str],
        names_above: set[str] | None,
    ) -> _Cell:
        source = "\n".join(lines_of(code or ""))
        masked = mask(source)
        parsed, error = _parse(masked.text)
        tree = parsed if masked.cell_magic is None else None
        lines = lines_of(source)
        comments, code_lines = _scan(masked.text)
        comment_lines = {c.line for c in comments if not _PRAGMA.match(c.text)}
        for stmt in tree.body if tree else ():  # a bare string statement is prose, not code
            if isinstance(stmt, ast.Expr) and isinstance(_constant(stmt.value), str):
                prose = set(range(stmt.lineno, (stmt.end_lineno or stmt.lineno) + 1))
                comment_lines |= prose
                code_lines -= prose
        if masked.cell_magic not in (None, *PYTHON_CELL_MAGICS):  # %%bash, %%sql: not Python
            code_lines = {n for n, line in enumerate(lines, 1) if line.strip()}
            comment_lines = set()
        code_text = list(lines)
        for comment in comments:
            code_text[comment.line - 1] = code_text[comment.line - 1][: comment.col]
        return cls(
            cfg=cfg,
            lines=lines,
            masked=masked,
            tree=tree,
            parse_error=error,
            flow=flow_of(source, masked, parsed),
            comments=comments,
            code_lines=code_lines,
            comment_lines=comment_lines,
            code_text="\n".join(code_text),
            title=title,
            intent=intent,
            bullets=bullets,
            names_above=names_above,
        )

    @property
    def python(self) -> bool:
        return self.masked.cell_magic is None or self.masked.cell_magic in PYTHON_CELL_MAGICS

    @functools.cached_property
    def secrets(self) -> secret_scan.Scan:
        """What would show a secret; L011 and L014 share one scan."""
        return secret_scan.scan(self.masked, self.lines, self.tree, self.names_above)

    @property
    def empty(self) -> bool:
        return not any(line.strip() and not line.strip().startswith("#") for line in self.lines)

    def segment(self, node: ast.AST) -> str:
        return ast.get_source_segment(self.masked.text, node) or ""

    def line(self, node: ast.stmt | ast.ExceptHandler) -> str:
        return self.lines[node.lineno - 1]


def lint_cell(
    code: str,
    *,
    title: str | None,
    notes: list[str] | str | None,
    intent: str | None,
    cfg: Config,
    require_note: bool,
    require_intent: bool,
    kernel_python: tuple[int, int] | None,
    names_above: set[str] | None,
) -> LintReport:
    clean_title = normalize_title(title) if title and title.strip() else None
    bullets = [re.sub(r"\s*\n\s*", " ", bullet) for bullet in split_notes(notes)]
    cell = _Cell.build(
        code, cfg, title=clean_title, intent=intent, bullets=bullets, names_above=names_above
    )
    issues: list[Issue] = []
    if cell.empty:
        message = "The cell has no code, only comments or blank lines."
        issues.append(_error("L001", "empty_code", message, "Pass the code to run in `code`."))
    elif cell.python:
        issues += _separators(cell)
    issues += _title_issues(title, require_note, cfg["markdown"]["title_max_words"])
    issues += _note_issues(notes, bullets, require_note, cfg)
    if require_intent and not (intent or "").strip():
        fix = "Pass `intent`: the user's ask, in one line."
        issues.append(_error("L005", "intent", "The intent is missing.", fix))
    if not cell.empty:
        issues += _syntax(cell, kernel_python)
    for rule, key, check in _CHECKS:
        level = _level(cfg, key)
        if level is None or (cell.empty and rule not in _NOTE_HINTS):
            continue
        try:
            found = check(cell)
        except Exception:  # a heuristic that trips on odd code must never break a write
            continue
        if found:
            message, fix, *question = found
            issues.append(Issue(rule, key, level, message, fix, *question))
    return LintReport(
        errors=sorted((i for i in issues if i.severity == "error"), key=lambda i: i.rule),
        hints=[i for i in issues if i.severity == "hint"],
        asks=[i for i in issues if i.severity == "ask"],
        code_lines=len(cell.code_lines),
        comment_lines=len(cell.comment_lines),
        defs=set(cell.flow.defs),
        uses=set(cell.flow.uses),
        parsed=cell.flow.parsed,
        title=clean_title,
        bullets=bullets,
    )


def _error(rule: str, key: str, message: str, fix: str) -> Issue:
    return Issue(rule, key, "error", message, fix)


def _level(cfg: Config, key: str) -> Severity | None:
    """The rule's configured level. Strict mode makes every hint an error and leaves an ask an
    ask: only the user decides an install. Only a rule that can ask asks; ``config.load`` refuses
    "ask" for the others, and one that gets it anyway is an error (design §6.4)."""
    level = cfg.rule(key)
    if level == "off":
        return None
    if level == "ask":
        return "ask" if key in ASK_RULES else "error"
    if level == "error" or cfg["lint"]["mode"] == "strict":
        return "error"
    return "hint"


# parsing -----------------------------------------------------------------------------------------
def _parse(
    text: str, feature_version: tuple[int, int] | None = None
) -> tuple[ast.Module | None, Exception | None]:
    try:
        return ast.parse(text, feature_version=feature_version), None
    except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
        return None, exc


def _scan(text: str) -> tuple[list[_Comment], set[int]]:
    """COMMENT tokens and code lines; lines past a tokenizer failure fall back to a line regex."""
    lines = text.split("\n")
    comments: list[_Comment] = []
    code: set[int] = set()
    reached = 0
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            row, col = tok.start
            if tok.type == tokenize.COMMENT:
                comments.append(_Comment(row, col, tok.string, not lines[row - 1][:col].strip()))
            elif tok.type not in _NOT_CODE:
                code.update(range(row, tok.end[0] + 1))
            reached = max(reached, tok.end[0])
    except Exception:  # 3.12+ raises TokenError/SyntaxError where 3.11 yielded ERRORTOKEN
        for row in range(reached + 1, len(lines) + 1):
            stripped = lines[row - 1].strip()
            if stripped.startswith("#"):
                col = len(lines[row - 1]) - len(lines[row - 1].lstrip())
                comments.append(_Comment(row, col, stripped, True))
            elif stripped:
                code.add(row)
    return comments, code


def _walk(node: ast.AST, *, into_defs: bool = True) -> Iterator[ast.AST]:
    """Pre-order walk in source order; ``into_defs=False`` skips function, lambda, class bodies."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        scope = isinstance(current, (*_FUNCS, ast.Lambda, ast.ClassDef))
        if into_defs or current is node or not scope:
            stack.extend(reversed(list(ast.iter_child_nodes(current))))


def _calls(cell: _Cell, *, into_defs: bool = True) -> Iterator[ast.Call]:
    if cell.tree is not None:
        yield from (n for n in _walk(cell.tree, into_defs=into_defs) if isinstance(n, ast.Call))


# text helpers ------------------------------------------------------------------------------------
def _quote(text: str, limit: int = 60) -> str:
    """One-line `code` for a message: whitespace collapsed, cut at ``limit`` characters."""
    joined = ""
    for piece in (p.strip() for p in text.strip().splitlines()):
        if piece:
            glue = "" if not joined or piece[0] in ".)]}" or joined[-1] in "([{" else " "
            joined += glue + piece
    if len(joined) > limit:
        joined = joined[: limit - 1].rstrip() + "…"
    return f"`{joined}`"


def _names(names: list[str], limit: int = 3) -> str:
    shown = ", ".join(f"`{n}`" for n in names[:limit])
    return shown + (f" and {len(names) - limit} more" if len(names) > limit else "")


def _more(count: int) -> str:
    return f" (+{count - 1} more)" if count > 1 else ""


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _constant(node: ast.AST | None) -> object:
    return node.value if isinstance(node, ast.Constant) else None


def _literal_text(node: ast.AST | None) -> str:
    """Words a string expression spells out: literals, f-string text, ``+``/``%``/``.format``."""
    value = _constant(node)
    if isinstance(value, str):
        return value
    if isinstance(node, ast.JoinedStr):
        parts = (_literal_text(v) if isinstance(v, ast.Constant) else " X " for v in node.values)
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
        return f"{_literal_text(node.left)} {_literal_text(node.right)}".strip()
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return _literal_text(node.func.value) if node.func.attr == "format" else ""
    return ""


def _func_name(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def _does_nothing(stmt: ast.stmt) -> bool:
    """``pass``, ``continue``, ``...`` or ``None`` on its own."""
    if isinstance(stmt, (ast.Pass, ast.Continue)):
        return True
    value = stmt.value if isinstance(stmt, ast.Expr) else None
    return isinstance(value, ast.Constant) and value.value in (None, Ellipsis)


# hard rules --------------------------------------------------------------------------------------
def _separators(cell: _Cell) -> list[Issue]:
    for comment in cell.comments:
        if comment.alone and any(p.match(comment.text) for p in CELL_SEPARATORS):
            message = (
                f"The code contains a cell separator ({_quote(comment.text)}), "
                "which would split it into several cells."
            )
            return [_error("L002", "cell_separator", message, _SEPARATOR_FIX)]
    return []


def _title_issues(raw: str | None, required: bool, max_words: int) -> list[Issue]:
    title = normalize_title(raw or "")
    words = count_words(title)
    if raw is None or not raw.strip():
        problem = "The note needs a title." if required else None
    elif re.search(r"[\r\n]", raw.strip()):
        problem = f"The title spans several lines: {_quote(raw)}."
    elif words == 0:
        problem = f"The title has no words: {_quote(raw)}."
    elif words > max_words:
        problem = f"The title has {words} words (max {max_words}): {_quote(title)}."
    elif _TITLE_MARKUP.search(title) or _HTML.search(title):
        problem = f"The title contains Markdown or HTML: {_quote(title)}."
    else:
        return []
    fix = (
        f"Pass a plain-text title of at most {max_words} words on one line, "
        "without '#', backticks or HTML."
    )
    return [_error("L003", "title", problem, fix)] if problem else []


def _note_issues(
    notes: list[str] | str | None, bullets: list[str], required: bool, cfg: Config
) -> list[Issue]:
    if not notes and not required:
        return []
    md = cfg["markdown"]
    low, high, max_words = md["notes_min"], md["notes_max"], md["bullet_max_words"]
    problems: list[str] = []
    if not low <= len(bullets) <= high:
        found = _plural(len(bullets), "bullet") if bullets else "no bullets"
        problems.append(f"The note has {found}; it needs {low}–{high}.")
    long = [b for b in bullets if count_words(b) > max_words]
    if long:
        words = count_words(long[0])
        problems.append(
            f"A bullet has {words} words (max {max_words}): {_quote(long[0])}{_more(len(long))}."
        )
    markup = _note_markup(notes, bullets)
    if markup:
        problems.append(f"The bullets contain {markup[0]}: {_quote(markup[1])}.")
    fix = (
        f"Pass {low}–{high} plain-text bullets saying what the cell does and why, each at most "
        f"{max_words} words, without headings, code fences, images, HTML or sub-bullets."
    )
    return [_error("L004", "notes", problem, fix) for problem in problems]


def _note_markup(notes: list[str] | str | None, bullets: list[str]) -> tuple[str, str] | None:
    if not notes:
        return None
    one_string = isinstance(notes, str)
    items = [textwrap.dedent(notes)] if isinstance(notes, str) else [str(item) for item in notes]
    for item in items:
        lines = [line for line in item.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if _HEADING.match(normalize_bullet(line)):
                return "a heading", line
            if _FENCE.search(line):
                return "a code fence", line
            if "![" in line:
                return "an image", line
            if _HTML.search(line):
                return "HTML", line
            # A string is one bullet per line, so only indentation nests; in a list item any
            # marker on a later line starts a sub-list.
            indented = bool(_NESTED_ITEM.match(line)) and (index > 0 or not one_string)
            if indented or (not one_string and index > 0 and _LIST_MARKER.match(line.strip())):
                return "a nested list", line
    return next((("a nested list", b) for b in bullets if _LIST_MARKER.match(b)), None)


def _syntax(cell: _Cell, kernel: tuple[int, int] | None) -> list[Issue]:
    """L007: hard only when the kernel's Python is known and no newer than ours (D12)."""
    if cell.masked.cell_magic is not None:
        return []
    ours = sys.version_info[:2]
    version = (3, kernel[1]) if kernel and len(kernel) >= 2 and kernel[0] == 3 else None
    known = version is not None and version <= ours
    error = cell.parse_error
    if version is not None and known and version != ours:
        _, error = _parse(cell.masked.text, version)
    if error is None:
        return []
    if isinstance(error, (RecursionError, MemoryError)):
        message = "The cell is nested too deeply for nh to check its syntax."
        return [Issue("L007", "syntax", "hint", message, "Flatten the deepest expression.")]
    detail = error.msg if isinstance(error, SyntaxError) else str(error)
    detail = _LINE_REF.sub("", detail) + _error_place(cell, error)
    suspect = isinstance(error, SyntaxError) and error.lineno in cell.masked.magic_lines
    if version is not None and known and not suspect:
        message = f"Python {version[0]}.{version[1]} can't parse this cell: {detail}."
        return [_error("L007", "syntax", message, "Fix the syntax error and call again.")]
    if suspect:
        why = "this may be nh misreading IPython syntax"
    elif version is None:
        why = "the kernel's Python version is unknown, so it may still run there"
    else:
        why = f"the kernel runs Python {version[0]}.{version[1]}, which may accept it"
    message = f"nh's Python {ours[0]}.{ours[1]} can't parse this cell ({detail}); {why}."
    fix = "Check the syntax; if it is valid on the kernel's Python, nothing needs to change."
    return [Issue("L007", "syntax", "hint", message, fix)]


def _error_place(cell: _Cell, error: Exception) -> str:
    lineno = getattr(error, "lineno", None)
    if isinstance(lineno, int) and 0 < lineno <= len(cell.lines) and cell.lines[lineno - 1].strip():
        return f" at {_quote(cell.lines[lineno - 1])}"
    last = next((line for line in reversed(cell.lines) if line.strip()), "")
    return f" near {_quote(last)}" if last else ""


def _notebook_write(cell: _Cell) -> Finding | None:
    match = next((m for p in CELL_NOTEBOOK_WRITES if (m := p.search(cell.code_text))), None)
    code = match.group(0) if match else _ast_notebook_write(cell)
    if code is None:
        return None
    return (
        f"The code writes a notebook file ({_quote(code)}).",
        "Don't write notebooks from code; nh writes cells for you.",
    )


def _ast_notebook_write(cell: _Cell) -> str | None:
    """``open("x.ipynb", "w")`` and ``Path(p).write_text``, which the shared patterns miss."""
    if cell.tree is None:
        return None
    paths = {  # nb_path = "reports/x.ipynb"
        stmt.targets[0].id: _path_text(stmt.value, {})
        for stmt in cell.tree.body
        if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)
    }
    for call in _calls(cell):
        name, target, writes = _func_name(call.func), "", False
        if name == "open" and call.args:
            target = _path_text(call.args[0], paths)
            mode = _constant(call.args[1] if len(call.args) > 1 else _keyword(call, "mode"))
            writes = isinstance(mode, str) and bool(set(mode) & set("wax+"))
        elif name in ("write_text", "write_bytes") and isinstance(call.func, ast.Attribute):
            target, writes = _path_text(call.func.value, paths), True
        if writes and target.lower().endswith(".ipynb"):
            return cell.segment(call)
    return None


def _path_text(node: ast.AST | None, paths: dict[str, str]) -> str:
    """The literal path an expression spells: "x.ipynb", Path("x.ipynb"), base / "x.ipynb"."""
    if isinstance(node, ast.Name):
        return paths.get(node.id, "")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _path_text(node.right, paths)
    if isinstance(node, ast.Call) and node.args and _func_name(node.func) in ("Path", "join"):
        return _path_text(node.args[-1], paths)
    return _literal_text(node).strip()


def _package_install(cell: _Cell) -> Finding | None:
    """L009, an ask by default (design §6.4): its message names what each package command of the
    cell does (installs, adds, removes) and to which packages, and its question says what keeps
    an install: the next env sync removes a kernel-only one."""
    text = cell.code_text
    patterns = [CELL_PACKAGE_INSTALL, _MAGIC_INSTALL, _OS_INSTALL]
    shell = cell.masked.cell_magic in _SHELL_CELL_MAGICS
    if shell:
        patterns.append(_SHELL_INSTALL)
    found = _line_starts(text, patterns)
    if not found:
        return None
    lines: list[str] = []
    kinds: dict[str, list[_PackageCommand]] = {}
    start = 0
    for line in text.split("\n"):  # detection's lines, and any other line with a package command
        commands: list[_PackageCommand] = []
        if (
            start in found
            or _COMMAND_LINE.search(line)
            or (shell and _SHELL_PACKAGE_LINE.match(line))
        ):
            commands = list(_package_commands(line))  # each line is scanned once
        if start in found or commands:
            lines.append(line)
            for command in commands:
                kinds.setdefault(command.kind, []).append(command)
        start += len(line) + 1
    if not kinds:
        kinds["install"] = []
    conda = cell.cfg["project"].get("env_manager") == "conda"
    texts = [_package_text(kind, commands, conda) for kind, commands in kinds.items()]
    if list(kinds) == ["install"]:
        lead = "Remove the install"
    else:
        lead = "Remove it" if len(kinds) == 1 else "Remove the package commands"
    tail = ": the next env sync removes kernel-only installs." if "install" in kinds else "."
    shown = f"{_quote(lines[0])}{_more(len(lines))}"
    return (
        f"The cell {'; it also '.join(t[0] for t in texts)} ({shown}).",
        f"{lead} and ask the user; after a yes, {', then '.join(t[2] for t in texts)} through "
        f"Bash{tail}",
        "; it also ".join(t[1] for t in texts),
    )


def _line_starts(text: str, patterns: list[re.Pattern[str]]) -> set[int]:
    """Where each line that one of ``patterns`` matches starts."""
    starts: set[int] = set()
    for pattern in patterns:
        for match in pattern.finditer(text):
            at = match.start() + len(match.group(0)) - len(match.group(0).lstrip())
            starts.add(text.rfind("\n", 0, at) + 1)
    return starts


@dataclass
class _PackageCommand:
    """One package command of an L009 line (design §6.4)."""

    kind: str  # install | uv-add | remove | uv-remove
    packages: list[str]
    editables: list[str]
    files: list[str]  # requirement files
    index: list[tuple[str, str]]  # pip's and uv's index options, as written: (option, value)
    channels: list[str]  # conda's


def _package_commands(line: str) -> Iterator[_PackageCommand]:
    """The package commands of one line, split at ``&&``, ``||``, ``;`` and `` |``: a tool word,
    its verb, and the words after it up to where the command ends."""
    for part in _COMMAND_SPLIT.split(line):
        command = _COMMAND.search(part)
        if command is None:
            continue
        conda = command["tool"].lower() in _CONDA_TOOLS
        project = command["tool"].lower() == "uv"  # `uv pip install` matches at its `pip`
        if command["verb"].lower() in ("uninstall", "remove"):
            kind = "uv-remove" if project else "remove"
        else:
            kind = "uv-add" if project and command["verb"].lower() == "add" else "install"
        rest = part[command.end() :]
        end = _COMMAND_END.search(rest)
        rest = rest[: end.start()] if end else rest
        found = _PackageCommand(kind, [], [], [], [], [])
        seen: set[str] = set()  # found.packages as a set: the scan stays linear
        option: str | None = None
        for word in (w.strip(",") for w in _INSTALL_WORD.findall(rest)):
            if option is not None:
                _option_value(found, option, word, conda)
                option = None
            elif word.startswith("-"):
                name, eq, value = word.partition("=")
                if eq:
                    _option_value(found, name, value, conda)
                elif _VALUE_OPTION.fullmatch(name) and not (conda and name == "-f"):
                    option = name  # conda's -f is --force, which takes no value
            elif any(ch.isalnum() for ch in word) and word not in seen:
                seen.add(word)
                found.packages.append(word)
        yield found


def _option_value(found: _PackageCommand, option: str, value: str, conda: bool) -> None:
    """Keep the value of an option L009 names; pip's -c (constraints) and the rest are dropped."""
    if not value:
        return
    if _REQUIREMENT_OPTION.fullmatch(option):
        found.files.append(value)
    elif _EDITABLE_OPTION.fullmatch(option):
        found.editables.append(value)
    elif conda and _CHANNEL_OPTION.fullmatch(option):
        found.channels.append(value)
    elif not conda and _INDEX_OPTION.fullmatch(option):
        found.index.append((option, value))


def _package_text(kind: str, commands: list[_PackageCommand], conda: bool) -> tuple[str, str, str]:
    """(message clause, question clause, fix part) of one kind of package command."""
    names: list[str] = []
    targets: list[str] = []  # the suggested command's words
    packages: list[str] = []
    options: list[str] = []
    option_set: set[str] = set()
    files = False
    seen: set[tuple[str, str]] = set()
    for command in commands:
        mine: list[str] = []
        for what, value in (
            *(("package", p) for p in command.packages),
            *(("editable", e) for e in command.editables),
            *(("file", f) for f in command.files),
        ):
            if (what, value) in seen:
                continue
            seen.add((what, value))
            if what == "package":
                mine.append(f"`{value}`")
                targets.append(_shell_word(value))
                packages.append(_shell_word(value))
            elif what == "editable":
                mine.append(f"`{value}` (editable)")
                targets += ["--editable", _shell_word(value)]
            else:
                mine.append(f"the packages in `{value}`")
                targets += ["-r", _shell_word(value)]
                files = True
        for option, value in command.index:
            pair = f"{option} {_shell_word(value)}"
            if pair not in option_set:
                option_set.add(pair)
                options.append(pair)
        sources = [f"`{v}`" for _, v in command.index] + [
            f"channel `{c}`" for c in command.channels
        ]
        if mine and sources:
            mine[-1] += f" (from {_and(sources)})"
        names += mine
    named = _and(names) if names else "packages"
    them = "it" if len(names) == 1 and not files else "them"
    add = " ".join([*(targets or ["<package>"]), *options])
    remove = " ".join(packages or ["<package>"])
    if kind == "install":
        message = f"installs {named} into the kernel only"
        if conda:
            keeps = f"adding {them} to environment.yml keeps {them}"
            fix = f"add {named} to environment.yml and run `nhctl env sync`"
        else:
            keeps = f"`uv add {add}` keeps {them}"
            fix = f"install {them} with `uv add {add}`"
        return message, f"{message}, and the next env sync removes {them} ({keeps})", fix
    if kind == "uv-add":
        message = f"adds {named} to the project's dependencies"
        return message, f"{message} with `uv add`", f"run `uv add {add}`"
    if kind == "uv-remove":
        message = f"removes {named} from the project's dependencies"
        return message, f"{message} with `uv remove`", f"run `uv remove {remove}`"
    message = f"removes {named} from the kernel"
    if conda:
        return message, message, f"remove {named} from environment.yml and run `nhctl env sync`"
    return message, message, f"run `uv remove {remove}`"


def _shell_word(word: str) -> str:
    """``word`` as one shell argument, double-quoted when the shell would read it otherwise:
    L009's question sits inside E122's single quotes (design §6.4)."""
    if _SHELL_SAFE.fullmatch(word):
        return word
    return '"' + re.sub(r'([\\"$`])', r"\\\1", word) + '"'


def _and(parts: list[str]) -> str:
    """`a`, `b` and `c`."""
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _markdown_output(cell: _Cell) -> Finding | None:
    fix = "Put explanations in the note bullets or your reply; code should show data, not prose."
    limit = cell.cfg["markdown"]["title_max_words"]
    magic = cell.masked.cell_magic
    if magic in ("markdown", "html"):
        return f"`%%{magic}` writes prose into the notebook from code.", fix
    if magic == "latex":
        words = count_words("\n".join(cell.lines[min(cell.masked.magic_lines) :]))
        return (f"`%%latex` writes {words} words of prose.", fix) if words > limit else None
    if cell.tree is None:
        for match in _PROSE_CALL_TEXT.finditer(cell.code_text if cell.python else ""):
            if count_words(match.group(2)) > limit:
                return f"{_quote(match.group(0))} displays prose from code.", fix
        return None
    strings: dict[str, str] = {}  # `md = """..."""` then `Markdown(md)`
    for stmt in cell.tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name):
            strings[stmt.targets[0].id] = _literal_text(stmt.value)
    for call in _calls(cell):
        if _func_name(call.func) in _PROSE_CALLS:
            arg = call.args[0] if call.args else _keyword(call, "data")
            text = _literal_text(arg)
            if not text and isinstance(arg, ast.Name):
                text = strings.get(arg.id, "")
            words = count_words(text)
            if words > limit:
                return f"{_quote(cell.segment(call))} displays {words} words of prose.", fix
    return None


def _secret_print(cell: _Cell) -> Finding | None:
    """Code that would show an env var's value (design 6.7). Names the var, never a value."""
    hits = cell.secrets.hits
    if not hits:
        return None
    hit, taint = hits[0], hits[0].taint
    what = _secret_what(taint)
    if taint.holder:
        what = f"`{taint.holder}`, which holds {what}"
    if hit.last and hit.sink == taint.holder:  # the last line is the variable itself
        where = "The last line"
    else:
        where = f"The last line {_quote(hit.sink)}" if hit.last else _quote(hit.sink)
    return f"{where} would show {what}{_more(len(hits))}.", _secret_fix(taint)


def _secret_what(taint: secret_scan.Taint) -> str:
    if taint.whole:
        return "every value in `.env`" if taint.dotenv else "every env var's value"
    if taint.var and taint.dotenv:
        return f"the value of `{taint.var}` from `.env`"
    if taint.var:
        return f"the value of env var `{taint.var}`"
    return "a value from `.env`" if taint.dotenv else "an env var's value"


def _secret_fix(taint: secret_scan.Taint) -> str:
    holds_mapping = taint.kind == "mapping" and taint.holder
    if taint.whole or holds_mapping:  # a variable of values, e.g. env_lines, gets no sorted()
        source = "dotenv_values()" if taint.dotenv else "os.environ"
        names = taint.holder if holds_mapping else taint.mapping or source
        return (
            f"Show only the names, e.g. `sorted({names})`, or check one without its value, "
            f'e.g. `print("NAME" in {names})`.'
        )
    if taint.holder and not taint.var:
        holder = taint.holder
        return (
            f"Check it without showing the value, e.g. `print({holder} is not None)` "
            f"or `print(bool({holder}))`."
        )
    name = taint.var or "NAME"
    if taint.dotenv:
        values = taint.mapping or "dotenv_values()"
        return (
            f'Check it without showing the value, e.g. `print("{name}" in {values})` '
            f'or `print(bool({values}.get("{name}")))`.'
        )
    return (
        f'Check it without showing the value, e.g. `print("{name}" in os.environ)` '
        f'or `print(bool(os.getenv("{name}")))`.'
    )


# readability hints -------------------------------------------------------------------------------
def _secret_name(cell: _Cell) -> Finding | None:
    """A shown name that says it holds a secret (L014); L011 already names the tainted ones."""
    scan = cell.secrets
    reported = scan.reported if _level(cell.cfg, "secret_print") else set()
    found: dict[str, secret_scan.Shown] = {}  # name -> where it is shown first
    for shown in scan.shown:
        for name in secret_scan.secret_names(shown.expr):
            if name not in reported:
                found.setdefault(name, shown)
    if not found:
        return None
    name, shown = next(iter(found.items()))
    if shown.last and shown.sink == name:
        where = "The last line"
    else:
        where = f"The last line {_quote(shown.sink)}" if shown.last else _quote(shown.sink)
    return (
        f"{where} shows `{name}`, whose name says it holds a secret{_more(len(found))}.",
        f"Show whether it is set instead, e.g. `print(bool({name}))`, or leave it out of the output.",
    )


def _long_line(cell: _Cell) -> Finding | None:
    limit = cell.cfg["lint"]["max_line_length"]
    long = [line for line in cell.lines if len(line) > limit]
    if not long:
        return None
    return (
        f"{_quote(long[0])} is {len(long[0])} characters long (max {limit}){_more(len(long))}.",
        "Break long lines at a natural point, e.g. one method call or argument per line.",
    )


def _long_cell(cell: _Cell) -> Finding | None:
    limit = cell.cfg["lint"]["max_cell_lines"]
    count = sum(1 for line in cell.lines if line.strip())
    if count <= limit:
        return None
    return (
        f"The cell has {count} lines (max {limit}).",
        "Split it at a natural step and propose the rest as the next cell.",
    )


def _compound(stmt: ast.stmt) -> bool:
    return hasattr(stmt, "body") or isinstance(stmt, ast.Match)


def _child_blocks(node: ast.stmt) -> Iterator[tuple[list[ast.stmt], bool]]:
    """Statement lists under ``node``; the flag is False for an ``elif`` (same depth)."""
    for name in ("body", "orelse", "finalbody"):
        stmts = getattr(node, name, None)
        if isinstance(stmts, list) and stmts and isinstance(stmts[0], ast.stmt):
            elif_ = isinstance(node, ast.If) and name == "orelse" and len(stmts) == 1
            yield stmts, not (elif_ and isinstance(stmts[0], ast.If))
    for handler in getattr(node, "handlers", ()):
        yield handler.body, True
    for case in getattr(node, "cases", ()):
        yield case.body, True


def _deep_nesting(cell: _Cell) -> Finding | None:
    if cell.tree is None:
        return None
    limit = cell.cfg["lint"]["max_nesting"]
    fix = "Flatten it: filter early, name intermediate results, or split the work into steps."
    worst: tuple[int, ast.stmt] | None = None
    stack = [(s, int(_compound(s))) for s in reversed(cell.tree.body)]
    while stack:
        stmt, depth = stack.pop()
        if depth > limit and (worst is None or depth > worst[0]):
            worst = (depth, stmt)
        for block, deeper in reversed(list(_child_blocks(stmt))):
            for child in reversed(block):
                stack.append((child, depth + int(deeper and _compound(child))))
    if worst:
        return f"{_quote(cell.line(worst[1]))} sits {worst[0]} blocks deep (max {limit}).", fix
    for node in _walk(cell.tree):
        inner = (n for n in _walk(node) if n is not node)
        if isinstance(node, _COMPS) and any(isinstance(n, _COMPS) for n in inner):
            return f"{_quote(cell.segment(node))} nests one comprehension inside another.", fix
    return None


def _chain_links(node: ast.AST) -> int:
    links = 0
    while True:
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            links += 1
            node = node.func.value
        elif isinstance(node, (ast.Attribute, ast.Subscript)):
            node = node.value
        else:
            return links


def _long_chain(cell: _Cell) -> Finding | None:
    limit = cell.cfg["lint"]["max_chain"]
    call = max(_calls(cell), key=_chain_links, default=None)
    links = _chain_links(call) if call else 0
    if call is None or links <= limit:
        return None
    return (
        f"{_quote(cell.segment(call))} chains {links} method calls (max {limit}).",
        f"Break it into named steps of at most {limit} calls (e.g. `df_clean = …`).",
    )


def _comment_budget(cell: _Cell) -> Finding | None:
    comments = len(cell.comment_lines)
    if not cell.python or not comments:
        return None
    ratio = cell.cfg["lint"]["comment_ratio"]
    code = len(cell.code_lines)
    allowed = 0 if ratio <= 0 else max(1, code // ratio)
    if comments <= allowed:
        return None
    fix = "Keep only comments that explain a non-obvious why; put the rest in the note bullets."
    if allowed == 0:
        found = _plural(comments, "comment line")
        return f"The cell has {found}; this project asks for none (comment_ratio = 0).", fix
    return (
        f"The cell has {comments} comment lines for {code} lines of code (budget {allowed}).",
        fix,
    )


def _looks_like_code(text: str) -> bool:
    if not re.search(r"[(=.]", text):
        return False
    tree, _ = _parse(textwrap.dedent(text))
    if tree is None or not tree.body:
        return False
    for stmt in tree.body:
        if isinstance(stmt, ast.AnnAssign) and stmt.value is None:  # "note: ..." is prose
            return False
        value = stmt.value if isinstance(stmt, ast.Expr) else None
        if isinstance(value, (ast.Name, ast.Constant)):
            return False
        bare_call = isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
        if bare_call and not re.search(r"\w\(", text):  # "Step (1)" is prose, "print(x)" is code
            return False
    return True


def _commented_out_code(cell: _Cell) -> Finding | None:
    if not cell.python:
        return None
    groups: list[list[_Comment]] = []  # runs of whole-line comments
    for comment in cell.comments:
        if not comment.alone or _PRAGMA.match(comment.text):
            continue
        if any(p.match(comment.text) for p in CELL_SEPARATORS):
            continue
        if groups and groups[-1][-1].line == comment.line - 1:
            groups[-1].append(comment)
        else:
            groups.append([comment])
    hits: list[str] = []
    for group in groups:
        bodies = [re.sub(r"^#+ ?", "", c.text) for c in group]
        if len(group) > 1 and _looks_like_code("\n".join(bodies)):
            hits.append(group[0].text)
        else:
            hits += [
                c.text for c, body in zip(group, bodies, strict=True) if _looks_like_code(body)
            ]
    if not hits:
        return None
    return (
        f"{_quote(hits[0])} looks like code left in a comment{_more(len(hits))}.",
        "Delete commented-out code; nh's undo history keeps earlier versions.",
    )


def _multi_statement(cell: _Cell) -> Finding | None:
    if cell.tree is None:
        return None
    for node in _walk(cell.tree):
        header = node if isinstance(node, (ast.stmt, ast.ExceptHandler)) else None
        for name in ("body", "orelse", "finalbody"):
            stmts = getattr(node, name, None)
            if not (isinstance(stmts, list) and stmts and isinstance(stmts[0], ast.stmt)):
                continue
            if header and name == "body" and stmts[0].lineno == header.lineno:
                return _one_per_line(cell, header)
            for first, second in zip(stmts, stmts[1:], strict=False):
                if second.lineno == (first.end_lineno or first.lineno):
                    return _one_per_line(cell, second)
    return None


def _one_per_line(cell: _Cell, node: ast.stmt | ast.ExceptHandler) -> Finding:
    message = f"{_quote(cell.line(node))} puts several statements on one line."
    return message, "Put each statement on its own line."


def _star_import(cell: _Cell) -> Finding | None:
    module = None
    if cell.tree is not None:
        stars = (
            n.module or "."
            for n in _walk(cell.tree)
            if isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        )
        module = next(stars, None)
    elif cell.python and (match := _STAR_IMPORT.search(cell.code_text)):
        module = match.group(1)
    if module is None:
        return None
    return (
        f"`from {module} import *` hides where names come from.",
        "Import the names you use, or the module under a short alias.",
    )


def _bare_except(cell: _Cell) -> Finding | None:
    if cell.tree is not None:
        found = any(isinstance(n, ast.ExceptHandler) and n.type is None for n in _walk(cell.tree))
    else:
        found = cell.python and bool(_BARE_EXCEPT.search(cell.code_text))
    if not found:
        return None
    return (
        "A bare `except:` catches every error, including typos and interrupts.",
        "Catch the exception you expect, e.g. `except KeyError:`.",
    )


def _same_expr(a: ast.AST, b: ast.AST) -> bool:
    return re.sub(r"ctx=\w+\(\)", "", ast.dump(a)) == re.sub(r"ctx=\w+\(\)", "", ast.dump(b))


def _rerun_hit(cell: _Cell, node: ast.AST, outside: frozenset[str]) -> tuple[str, str] | None:
    """(code, name) when re-running ``node`` would change a value from an earlier cell again."""
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target = node.targets[0]
        name = base_name(target)
        if name in outside:
            if isinstance(target, ast.Name):
                reads = any(isinstance(n, ast.Name) and n.id == name for n in _walk(node.value))
            else:  # df["p"] = df["p"] / 100
                reads = any(_same_expr(n, target) for n in _walk(node.value))
            if reads:
                return cell.line(node), name
    elif isinstance(node, ast.AugAssign):
        name = base_name(node.target)
        if name in outside:
            return cell.line(node), name
    elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
        func = node.value.func
        name = base_name(func.value) if isinstance(func, ast.Attribute) else None
        if name in outside and _func_name(func) in _APPENDS:
            return cell.line(node), name
    return None


def _non_idempotent(cell: _Cell) -> Finding | None:
    if cell.tree is None:
        return None
    outside = cell.flow.uses  # read before this cell binds them: made by an earlier cell
    hits: list[str] = []
    for node in _walk(cell.tree, into_defs=False):
        if isinstance(node, ast.Call) and _constant(_keyword(node, "inplace")) is True:
            name = base_name(node.func)
            if name and _made_here(cell, name):
                continue  # the cell makes `name` first, so a re-run starts from scratch
            hits.append(f"{_quote(cell.segment(node))} changes `{name or 'the data'}` in place")
        elif hit := _rerun_hit(cell, node, outside):
            hits.append(f"{_quote(hit[0])} changes `{hit[1]}`, which an earlier cell made")
    if not hits:
        return None
    return (
        f"{hits[0]}, so re-running the cell applies it again{_more(len(hits))}.",
        "Write the result to a new name (e.g. `df_clean = df.dropna()`) so re-runs are safe.",
    )


def _made_here(cell: _Cell, name: str) -> bool:
    """The cell binds ``name`` to a new object before reading it (not ``view = df``)."""
    if cell.tree is None or name in cell.flow.uses or name not in cell.flow.fresh:
        return False
    for stmt in cell.tree.body:
        if isinstance(stmt, ast.Assign):
            targets, value = stmt.targets, stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        else:
            continue
        stores = (n for t in targets for n in ast.walk(t) if isinstance(n, ast.Name))
        if any(n.id == name for n in stores):
            return not isinstance(value, (ast.Name, ast.Attribute))  # an alias shares the object
    return True


def _apply_lambda(cell: _Cell) -> Finding | None:
    for call in _calls(cell):
        name = _func_name(call.func)
        if name == "apply":
            func = call.args[0] if call.args else _keyword(call, "func")
            if isinstance(func, ast.Lambda) and _constant(_keyword(call, "axis")) in (1, "columns"):
                return (
                    f"{_quote(cell.segment(call))} runs Python row by row: slow and hard to read.",
                    "Use vectorised column operations (arithmetic on columns, `.map`, `np.select`).",
                )
        elif name == "where" and base_name(call.func) in ("np", "numpy"):
            inner = (n for arg in call.args for n in _walk(arg) if isinstance(n, ast.Call))
            if any(_func_name(n.func) == "where" for n in inner):
                return (
                    f"{_quote(cell.segment(call))} nests `np.where` calls, which is hard to read.",
                    "Use `np.select` with named conditions and choices.",
                )
    return None


def _cryptic_name(cell: _Cell) -> Finding | None:
    if cell.tree is None:
        return None
    names: set[str] = set()
    for node in _walk(cell.tree, into_defs=False):
        if isinstance(node, (*_FUNCS, ast.ClassDef)):
            names.add(node.name)
        targets = node.targets if isinstance(node, ast.Assign) else []
        if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            targets = [node.target]
        for target in targets:
            stores = (n for n in _walk(target) if isinstance(n, ast.Name))
            names |= {n.id for n in stores if isinstance(n.ctx, ast.Store)}
    single = {n for n in names if len(n) == 1 and n not in ("X", "y", "_")}
    bad = sorted(single | {n for n in names if _CRYPTIC.fullmatch(n)})
    if not bad:
        return None
    return (
        f"Names like {_names(bad)} don't say what they hold.",
        "Rename them after their content, e.g. `sales_2024` or `df_clean`.",
    )


def _early_def(cell: _Cell) -> Finding | None:
    if cell.tree is None:
        return None
    for node in cell.tree.body:
        if not isinstance(node, (*_FUNCS, ast.ClassDef)) or node.decorator_list:
            continue
        inside = {id(n) for n in _walk(node)}
        refs = [n for n in _walk(cell.tree) if isinstance(n, ast.Name) and n.id == node.name]
        uses = sum(1 for n in refs if id(n) not in inside)
        if uses < 2:
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            where = "once" if uses == 1 else "nowhere"
            return (
                f"`{kind} {node.name}` is used {where} in this cell; a function pays off once "
                "code is reused.",
                "Inline the code for now and make it a function when a second cell needs it.",
            )
    return None


def _display_points(cell: _Cell) -> dict[str, int] | None:
    """How many separate outputs the cell shows, by kind; None when nh can't tell."""
    if cell.tree is None:
        return None
    counts = {"print": 0, "figure": 0, "last line": 0, "magic": 0}
    created = draws = shows = plt_shows = 0
    counted: set[int] = set()
    for call in _calls(cell, into_defs=False):
        func, name = call.func, _func_name(call.func) or ""
        owner = func.value if isinstance(func, ast.Attribute) else None
        root = base_name(owner) if owner else None
        opens_figure = root == "plt" and name in _PLT_FIGURE
        if name in _PRINTS or (name == "info" and root and not call.args):
            counts["print"] += 1
        elif opens_figure or (root == "sns" and _SNS_FIGURE.fullmatch(name)):
            created += 1
        elif root == "plt" and _PLT_DRAW.fullmatch(name):
            draws += 1
        elif root == "sns":
            draws += not _SNS_SETUP.fullmatch(name)
        elif owner and (name in _FRAME_PLOTS or _func_name(owner) == "plot"):
            on_axes = (root or "").startswith("ax") or _keyword(call, "ax") is not None
            draws += on_axes  # `ax.plot`, `df.plot(ax=ax)`; otherwise pandas opens a figure
            created += not on_axes
        elif name == "show":
            shows += root != "plt"  # plt.show() only flushes figures counted above
            plt_shows += root == "plt"
        elif _FIGURE_CALL.fullmatch(name):
            on_axes = (root or "").startswith("ax") or _keyword(call, "ax") is not None
            draws += on_axes
            created += not on_axes
        else:
            continue
        counted.add(id(call))
    counts["figure"] = max(created, 1 if draws else 0) + shows
    last = cell.tree.body[-1] if cell.tree.body else None
    if isinstance(last, ast.Expr) and id(last.value) not in counted and not _suppressed(cell, last):
        counts["last line"] = int(not _does_nothing(last))
    masked_lines = cell.masked.text.split("\n")
    for number in cell.masked.magic_lines:
        raw = cell.lines[number - 1]
        magic = _MAGIC_NAME.match(raw)
        if not masked_lines[number - 1].strip() or _ASSIGNED_MAGIC.match(raw):
            continue  # continuation line, or `x = !cmd` (captured, not shown)
        counts["magic"] += not (magic and _SILENT_MAGIC.fullmatch(magic.group(1)))
    if plt_shows and not sum(counts.values()):
        counts["figure"] = 1  # plt.show() of a figure drawn some way nh doesn't know
    return counts


def _suppressed(cell: _Cell, stmt: ast.stmt) -> bool:
    """A trailing ``;`` stops Jupyter from showing the last expression."""
    line = cell.masked.text.split("\n")[(stmt.end_lineno or stmt.lineno) - 1]
    after = line.encode("utf-8", "surrogatepass")[stmt.end_col_offset or 0 :]
    return after.decode("utf-8", "ignore").lstrip().startswith(";")


def _is_setup(stmt: ast.stmt) -> bool:
    """Imports, definitions and constants: nothing to show is expected."""
    if isinstance(stmt, (ast.Import, ast.ImportFrom, ast.Pass, *_FUNCS, ast.ClassDef)):
        return True
    if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
        try:
            ast.literal_eval(stmt.value)
        except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError):
            return False
        return True
    return False


def _no_visible_output(cell: _Cell) -> Finding | None:
    counts = _display_points(cell)
    if cell.tree is None or counts is None or sum(counts.values()):
        return None
    if all(map(_is_setup, cell.tree.body)):
        return None
    return (
        "Nothing in this cell shows a result, so the user has nothing to check.",
        "End the cell with the key value, e.g. `df_clean.shape` or `df_clean.head()`.",
    )


def _many_outputs(cell: _Cell) -> Finding | None:
    counts = _display_points(cell) or {}
    total = sum(counts.values())
    if total < 3:
        return None
    parts = [_plural(n, kind) if kind != "last line" else kind for kind, n in counts.items() if n]
    return (
        f"The cell shows {total} outputs ({', '.join(parts)}), which looks like {total} steps.",
        "Keep the one output that answers the ask and propose the rest as next cells.",
    )


def _hides(node: ast.AST) -> str | None:
    """What ``node`` keeps from the user: warnings, errors or log messages."""
    if isinstance(node, ast.Call):
        name = _func_name(node.func)
        values = [*node.args, *(k.value for k in node.keywords)]
        if name in ("filterwarnings", "simplefilter", "seterr"):
            ignores = any(_constant(v) == "ignore" for v in values)
            return "warnings" if ignores and not _one_warning(node, name) else None
        if name == "set_option" and node.args:
            return "warnings" if _constant(node.args[0]) == "mode.chained_assignment" else None
        if name == "suppress":
            return "errors"
        return "log messages" if name == "disable" and base_name(node.func) == "logging" else None
    if isinstance(node, ast.ExceptHandler):
        return "errors" if all(map(_does_nothing, node.body)) else None
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Attribute):
        return "warnings" if node.targets[0].attr == "chained_assignment" else None
    return None


def _one_warning(call: ast.Call, name: str | None) -> bool:
    """``filterwarnings``/``simplefilter`` narrowed to one category or message, not all warnings."""
    if name == "seterr":
        return False
    positional = {
        "filterwarnings": ("action", "message", "category"),
        "simplefilter": ("action", "category"),
    }
    given = dict(zip(positional.get(name or "", ()), call.args, strict=False))
    given.update({k.arg: k.value for k in call.keywords if k.arg})
    message, category = _constant(given.get("message")), given.get("category")
    if isinstance(message, str) and message.strip():
        return True
    return category is not None and _func_name(category) not in ("Warning", None)


def _hidden_warnings(cell: _Cell) -> Finding | None:
    fix = "Remove it, or silence one specific warning and say why in a bullet."
    if cell.masked.cell_magic == "capture":
        return "`%%capture` hides the cell's output and errors from the user.", fix
    for node in _walk(cell.tree) if cell.tree else ():
        hidden = _hides(node)
        if hidden:
            return f"{_quote(cell.segment(node))} hides {hidden} the user should see.", fix
    return None


def _prose_print(cell: _Cell) -> Finding | None:
    for call in _calls(cell):
        if isinstance(call.func, ast.Name) and call.func.id == "print":
            words = count_words(" ".join(_literal_text(arg) for arg in call.args))
            if words > _MAX_PRINT_WORDS:
                return (
                    f"{_quote(cell.segment(call))} prints {words} words of prose.",
                    "Print values with short labels; put explanations in the bullets or your reply.",
                )
    return None


def _kernel_only_name(cell: _Cell) -> Finding | None:
    above, flow = cell.names_above, cell.flow
    if above is None or EVERYTHING in above or not flow.parsed or flow.open:
        return None
    candidates = flow.uses - above - flow.bound - _BUILTINS
    missing = sorted(n for n in candidates if not _IPYTHON_NAME.fullmatch(n))
    if not missing:
        return None
    one = len(missing) == 1
    return (
        f"{_names(missing)} {'is' if one else 'are'} not defined by any cell above (only the "
        f"kernel has {'it' if one else 'them'}), so the notebook won't run top to bottom.",
        "Define it in this cell or an earlier one, or tell the user which cell it came from.",
    )


def _intent_too_long(cell: _Cell) -> Finding | None:
    intent = (cell.intent or "").strip()
    if len(intent) <= _MAX_INTENT_CHARS:
        return None
    return (
        f"The intent is {len(intent)} characters (max {_MAX_INTENT_CHARS}): {_quote(intent)}.",
        "Shorten `intent` to the user's ask in one line.",
    )


def _long_bullet(cell: _Cell) -> Finding | None:
    md = cell.cfg["markdown"]
    soft, hard = md["bullet_hint_words"], md["bullet_max_words"]
    long = [b for b in cell.bullets if soft < count_words(b) <= hard]
    if not long:
        return None
    return (
        f"A bullet has {count_words(long[0])} words (aim for {soft} or fewer): "
        f"{_quote(long[0])}{_more(len(long))}.",
        "Split it or cut it to one short point.",
    )


# Configurable hard rules first, then hints, most useful first: results show only a few hints.
_CHECKS: list[tuple[str, str, Callable[[_Cell], Finding | None]]] = [
    ("L008", "notebook_write", _notebook_write),
    ("L009", "package_install", _package_install),
    ("L010", "markdown_output", _markdown_output),
    ("L011", "secret_print", _secret_print),
    ("L014", "secret_name", _secret_name),
    ("L120", "kernel_only_name", _kernel_only_name),
    ("L111", "non_idempotent", _non_idempotent),
    ("L118", "hidden_warnings", _hidden_warnings),
    ("L116", "no_visible_output", _no_visible_output),
    ("L117", "many_outputs", _many_outputs),
    ("L119", "prose_print", _prose_print),
    ("L109", "bare_except", _bare_except),
    ("L104", "long_chain", _long_chain),
    ("L103", "deep_nesting", _deep_nesting),
    ("L112", "apply_lambda", _apply_lambda),
    ("L106", "commented_out_code", _commented_out_code),
    ("L105", "comment_budget", _comment_budget),
    ("L113", "cryptic_name", _cryptic_name),
    ("L114", "early_def", _early_def),
    ("L108", "star_import", _star_import),
    ("L107", "multi_statement", _multi_statement),
    ("L101", "long_line", _long_line),
    ("L102", "long_cell", _long_cell),
    ("L122", "intent_too_long", _intent_too_long),
    ("L123", "long_bullet", _long_bullet),
]
