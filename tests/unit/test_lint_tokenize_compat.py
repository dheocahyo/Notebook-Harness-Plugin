"""Tokenizer and parser differences between Python 3.11 and 3.12+ never crash lint or hard-reject.

3.12 moved ``tokenize`` onto the C tokenizer: ``!`` and ``?`` became OP tokens (ERRORTOKEN before),
f-strings split into FSTRING_* tokens, and bad input raises TokenError instead of yielding
ERRORTOKEN. Run this file on every supported interpreter.
"""

from __future__ import annotations

import io
import sys
import tokenize
from typing import Any

import pytest

from nh_gateway.config import load
from nh_gateway.dataflow import defs_uses
from nh_gateway.lint.lint import LintReport, lint_cell

OURS = sys.version_info[:2]
NEW_TOKENIZER = OURS >= (3, 12)


def lint(code: str, kernel: Any = None) -> LintReport:
    return lint_cell(
        code,
        title="Check the corpus cell",
        notes=["First point about the cell.", "Second point about the cell."],
        intent="check the tokenizer",
        cfg=load(None),
        require_note=True,
        require_intent=True,
        kernel_python=kernel,
        names_above=set(),
    )


def token_types(source: str) -> list[str]:
    return [
        tokenize.tok_name[t.type] for t in tokenize.generate_tokens(io.StringIO(source).readline)
    ]


# The premises: if these ever change, the tests below still hold, but the notes above need updating.
def test_bang_token_differs_by_version() -> None:
    assert token_types("!ls\n")[0] == ("OP" if NEW_TOKENIZER else "ERRORTOKEN")


def test_hash_in_fstring_format_spec_is_never_a_comment() -> None:
    types = token_types('f"{y:#x}"\n')
    assert "COMMENT" not in types
    assert ("FSTRING_MIDDLE" in types) if NEW_TOKENIZER else (types[0] == "STRING")


def test_unterminated_single_quote_raises_only_on_new_tokenizer() -> None:
    if NEW_TOKENIZER:
        with pytest.raises(tokenize.TokenError):
            token_types("s = 'abc\n")
    else:
        assert "ERRORTOKEN" in token_types("s = 'abc\n")


# Corpus: odd but realistic cells. With the kernel's Python unknown, nothing may hard-reject.
CORPUS = [
    "!ls",
    "df?",
    "df.head??",
    "%matplotlib inline",
    "%%time\nx_total = sum(range(10))",
    "rows = [\n!ls\n]",  # a bang where masking can't reach it
    'label = f"{y:#x}"\nlabel',
    'label = f"{value:# %%}"\nlabel',
    'label = f"{d["k"]}"\nlabel',  # quote reuse: 3.12+ only
    "def first[T](items: list[T]) -> T:\n    return items[0]\nfirst",  # 3.12+ only
    's = """never closed\n# %%\n',
    "s = 'never closed\nvalue_total = 1\nvalue_total",
    "value_total = 1\0\nvalue_total",
    "if ready:\n\tvalue_a = 1\n        value_b = 2\n",
    "if ready:\n        value_a = 1\n    value_b = 2\n",
    "value_total = 1)\nvalue_total",
    "value_total = $cost\nvalue_total",
    "value_total = `cost`\nvalue_total",
    "(" * 250 + ")" * 250,
    "value_total = " + " + ".join(["part"] * 3000),
    "\ufeffvalue_total = 1\nvalue_total",
    "café_total = 'naïve ☕'\ncafé_total",
    "value_total = 1\r\n# %% \r\nvalue_total",
]


@pytest.mark.parametrize("code", CORPUS)
def test_corpus_never_crashes_or_hard_rejects_on_syntax(code: str) -> None:
    report = lint(code)
    assert "L007" not in [e.rule for e in report.errors]
    assert {e.rule for e in report.errors} <= {"L002"}  # the one corpus cell with a real separator
    defs_uses(code)


@pytest.mark.parametrize("code", CORPUS)
@pytest.mark.parametrize("kernel", [(3, 10), (3, 11), OURS, (3, 14)])
def test_corpus_never_crashes_with_known_kernels(code: str, kernel: tuple[int, int]) -> None:
    report = lint(code, kernel)
    for issue in report.errors + report.hints:
        assert issue.message and issue.fix


def test_separator_after_a_tokenizer_failure_is_still_found() -> None:
    # 3.12+ stops tokenizing at the unterminated string; the line fallback finds the separator.
    report = lint("s = 'never closed\n# %%\nvalue_total = 1\nvalue_total")
    assert "L002" in [e.rule for e in report.errors]


def test_fstring_format_spec_is_not_a_separator_or_comment() -> None:
    report = lint('label = f"{value:# %%}"\nlabel')
    assert "L002" not in [e.rule for e in report.errors]
    assert report.comment_lines == 0


def test_quote_reuse_depends_on_the_gateway_version() -> None:
    code = 'label = f"{d["k"]}"\nlabel'
    rules = [e.rule for e in lint(code, (3, 11)).errors]
    # A 3.12+ gateway parses it with 3.11's grammar flags, which don't cover the f-string change;
    # the kernel then reports the error when the cell runs.
    assert rules == ([] if NEW_TOKENIZER else ["L007"])


def test_type_parameters_follow_the_kernel_version() -> None:
    code = "def first[T](items: list[T]) -> T:\n    return items[0]\nfirst"
    assert "L007" in [e.rule for e in lint(code, (3, 11)).errors]
    newer_kernel = lint(code, (3, 12))
    if NEW_TOKENIZER:
        assert "L007" not in [i.rule for i in newer_kernel.errors + newer_kernel.hints]
    else:
        assert "L007" in [h.rule for h in newer_kernel.hints]


def test_unterminated_triple_quote() -> None:
    code = 's = """never closed\nvalue_total = 1'
    assert [h.rule for h in lint(code).hints if h.rule == "L007"] == ["L007"]
    assert [e.rule for e in lint(code, OURS).errors] == ["L007"]


def test_null_byte_is_a_syntax_error_for_a_known_kernel() -> None:
    assert [e.rule for e in lint("value_total = 1\0", OURS).errors] == ["L007"]


def test_help_and_bang_lines_are_code_lines() -> None:
    report = lint("!ls data\ndf?\n%matplotlib inline\nsales = 1\nsales")
    assert report.code_lines == 5
    assert report.comment_lines == 0
    assert report.parsed
