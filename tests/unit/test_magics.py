"""The stdlib IPython masker: magic lines become ``pass`` without moving any other line."""

from __future__ import annotations

import ast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from nh_gateway.lint.magics import lines_of, mask


def masked_lines(source: str) -> list[str]:
    return mask(source).text.split("\n")


@pytest.mark.parametrize(
    "line",
    ["%matplotlib inline", "!ls -la", "%pip install x", "?df", "??df", "/f 1", ",f a b", ";f a b"],
)
def test_escape_lines_become_pass(line: str) -> None:
    masked = mask(f"import pandas as pd\n{line}\nx = 1")
    assert masked.text.split("\n")[1].strip() == "pass"
    assert masked.magic_lines == {2}
    assert masked.cell_magic is None
    ast.parse(masked.text)


def test_masked_line_keeps_its_length_and_indent() -> None:
    lines = masked_lines("for i in range(3):\n    !echo {i}\n")
    assert lines[1].rstrip() == "    pass"
    assert len(lines[1]) == len("    !echo {i}")
    ast.parse("\n".join(lines))


@pytest.mark.parametrize("line", ["df?", "df.head??", "df.plot?  # help"])
def test_help_suffix_is_masked(line: str) -> None:
    masked = mask(f"if True:\n    {line}\n")
    assert masked.magic_lines == {2}
    ast.parse(masked.text)


@pytest.mark.parametrize(
    "source",
    [
        's = "what?"',
        "x = 1  # really?",
        "x = y if a else b  # ok?",
        "s = 'abc?",  # an unterminated string is Python's error, not a help lookup
    ],
)
def test_question_marks_in_strings_and_comments_are_code(source: str) -> None:
    assert mask(source).magic_lines == set()


def test_backslash_continued_magic_blanks_its_continuation() -> None:
    masked = mask("!pip install \\\n    pandas \\\n    numpy\nx = 1")
    assert masked.magic_lines == {1, 2, 3}
    assert [line.strip() for line in masked.text.split("\n")] == ["pass", "", "", "x = 1"]
    ast.parse(masked.text)


@pytest.mark.parametrize(
    "source",
    [
        "x = (1\n     % 2)",
        "mask = (df['a']\n        != 0)",
        "x = 5 \\\n    % 2",
        's = """\n!not a magic\n%neither\n"""',
        "s = '''a\n?b'''",
        "call(\n    a,\n    ,\n)",
        "d = {\n    'k': 1,\n    }\n",
    ],
)
def test_continuation_lines_are_not_masked(source: str) -> None:
    masked = mask(source)
    assert masked.magic_lines == set()
    assert masked.text == source


def test_time_magic_keeps_its_statement_for_dataflow() -> None:
    masked = mask("%time y = f(x)\n%time !ls\n%timeit g(y)")
    lines = masked.text.split("\n")
    assert lines[0].rstrip() == "y = f(x)"
    assert lines[1].strip() == "pass"
    assert lines[2].strip() == "pass"
    assert masked.magic_lines == {1, 2, 3}


def test_time_magic_inside_a_block_stays_indented() -> None:
    masked = mask("for i in range(3):\n    %time f(i)\n")
    assert masked.text.split("\n")[1].rstrip() == "    f(i)"
    ast.parse(masked.text)


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("files = !ls", "files = None"),
        ("a, b = %sx cmd", "a, b = None"),
        ("  env=%env", "  env = None"),
    ],
)
def test_assignment_magics_keep_their_target(line: str, expected: str) -> None:
    masked = mask(line)
    assert masked.text.rstrip() == expected
    assert masked.magic_lines == {1}


def test_comparison_is_not_an_assignment_magic() -> None:
    assert mask("x == 3").magic_lines == set()
    assert mask("x %= 3").magic_lines == set()


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("%%time\nx = 1", "time"),
        ("%%capture --no-stderr out\nprint(1)", "capture"),
        ("\n\n  %%bash\nls", "bash"),
        ("%%writefile out.py\nprint(1)", "writefile"),
        ("%%\nx", ""),
    ],
)
def test_cell_magic(source: str, name: str) -> None:
    masked = mask(source)
    assert masked.cell_magic == name
    header = next(i for i, line in enumerate(lines_of(source), 1) if line.strip())
    assert header in masked.magic_lines
    assert masked.text.split("\n")[header - 1].strip() == "pass"


def test_cell_magic_body_line_magics_are_masked() -> None:
    masked = mask("%%time\n%matplotlib inline\nx = 1")
    assert masked.cell_magic == "time"
    assert masked.magic_lines == {1, 2}
    ast.parse(masked.text)


def test_double_percent_after_first_line_is_a_line_magic() -> None:
    masked = mask("x = 1\n%%time\ny = 2")
    assert masked.cell_magic is None
    assert masked.magic_lines == {2}


def test_mixed_cell_parses_after_masking() -> None:
    source = "\n".join(
        [
            "%matplotlib inline",
            "!pip list",
            "import pandas as pd",
            "files = !ls data",
            "df = pd.read_csv(files[0])",
            "df.head?",
            "for col in df:",
            "    %time df[col].sum()",
            "summary = (df.describe()",
            "           % 1)",
        ]
    )
    masked = mask(source)
    assert masked.magic_lines == {1, 2, 4, 6, 8}
    ast.parse(masked.text)


def test_newlines_are_normalised() -> None:
    masked = mask("a = 1\r\n%ls\r\nb = 2\rc = 3")
    assert masked.text.split("\n")[0] == "a = 1"
    assert masked.magic_lines == {2}
    assert len(masked.text.split("\n")) == 4


def test_empty_source() -> None:
    masked = mask("")
    assert (masked.text, masked.cell_magic, masked.magic_lines) == ("", None, set())


@given(st.text(alphabet=st.sampled_from(list("ab %!?#\"'()[]{}\\=.:\n\t;,/")), max_size=200))
def test_mask_never_raises_and_keeps_line_count(source: str) -> None:
    masked = mask(source)
    assert len(masked.text.split("\n")) == len(lines_of(source))
    assert all(1 <= n <= len(lines_of(source)) for n in masked.magic_lines)
