"""tomlread's 3.9 reader (``_mini_loads``): it reads a text as ``tomllib`` does or refuses it
(design §6.9). The hooks, nhctl and the doctor use it where ``tomllib`` is missing, and C9a shows
what it reads to users (the doctor's preset and D171, ``nhctl preset``'s report)."""

from __future__ import annotations

import math
import tomllib
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from nh_gateway._shared import harness_toml, tomlread
from nh_gateway._shared.scaffold import core
from tests.unit.test_config import PRECEDENCE


def same(a: Any, b: Any) -> bool:
    """Equal, NaN equal to NaN, types too (``1`` isn't ``1.0`` or ``True``)."""
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    return type(a) is type(b) and a == b


def agrees(text: str) -> bool:
    """The 3.9 reader reads ``text`` as tomllib does, or refuses it; it refuses whatever tomllib
    refuses."""
    try:
        want: Any = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        want = None
    try:
        got: Any = tomlread._mini_loads(text)
    except ValueError:
        return True
    return want is not None and same(want, got)


# What the C9a reviews found the old reader misread, and what it must now read as tomllib does.
READS = [
    "[preset]\nlevel = '''senior'''\n",
    '[preset]\nlevel = """senior"""\n',
    'preset.level = "senior"\n',
    '["preset"]\nlevel = "senior"\n',
    "[ 'preset' ]\nlevel = 'senior'\n",
    '[lint]\n"comment_ratio" = 4\n',
    "lint.comment_ratio = 4\n",
    "lint . comment_ratio = 4\n",
    '[preset]\nlevel = "junior"\n\n[[guardrails]]\nlevel = "senior"\n',
    'lint.comment_ratio = 12\n[preset]\nlevel = "senior"\n',
    '"smile\\U0001F600" = 1\n["x\\u00e9"]\nk = "\\t"\n',
    "[a]\nb.c = 1\n[a.b.d]\n",  # a header may add a table under dotted keys' table
    "[a.b.c]\n[a]\nb.d = 1\n",  # dotted keys may extend a table a header only implied
    "[[a]]\nx = [1]\n[[a]]\n[a.x]\n",  # each [[a]] table starts fresh
    "[[a.b]]\nx = 1\n[[a.b]]\nx = 2\n[a.c]\n",
    "x = 0x1F\ny = 0o17\nz = 0b101\nw = 1_000\nv = -0\nu = +5\n",
    "f = 1.5\ng = 1e3\nh = -2.5E-3\ni = inf\nj = -nan\nk = 1_0.5_5\n",
    'a = [1, [2, "x"], "#", \'y\',]\nb = []\nc = [ ]\n',
    "s = '''it''s'''\nt = '''a'''''\nu = \"\"\"a\"\"\"\"\nv = ''\nw = \"\"\n",
    "x = \"a # b\"  # c\ny = '#'\n",
    "a = 1\r\nb = 2\r\n",
]

# Valid TOML the reader refuses rather than read (design §6.9's known gap), and invalid TOML
# the old reader accepted.
REFUSES = [
    'preset = { level = "senior" }\n',
    "d = 1979-05-27\n",
    "t = 07:32:00\n",
    'goal = """\nmulti\n"""\n',
    "x = [\n  1,\n]\n",
    "x = 1\nx = 2\n",
    "[a]\n[a]\n",
    "a.b = 1\n[a]\n",
    "[a]\nb = 1\n[a.b]\n",
    "[a.b]\n[a]\nb.c = 1\n",
    "x = 'a' b\n",
    'x = "a" "b"\n',
    "x = 01\n",
    "x = 1__0\n",
    "x = [1,,2]\n",
    "x = [,]\n",
    "x = 'a\x01'\n",
    "x = 1 # \x01\n",
    '"\\x41" = 1\n',
    "[lint\nmode = 1\n",
    "[[a] ]\n",
    "a\n",
    "= 1\n",
    "\ufeffversion = 1\n",
    "a = 1\rb = 2\n",
]


@pytest.mark.parametrize("text", READS)
def test_the_reader_reads_these_as_tomllib_does(text: str) -> None:
    assert same(tomlread._mini_loads(text), tomllib.loads(text)), text


@pytest.mark.parametrize("text", REFUSES)
def test_the_reader_refuses_these(text: str) -> None:
    with pytest.raises(ValueError):
        tomlread._mini_loads(text)


@pytest.mark.parametrize("toml", [toml for toml, _, _ in PRECEDENCE])
def test_the_reader_agrees_on_the_preset_and_the_budget(toml: str) -> None:
    """What the doctor, nhctl preset and C9b's SessionStart read on 3.9 is what the gateway
    reads (test_config.py's precedence table)."""
    text = f"version = 1\n{toml}"
    mini, real = tomlread._mini_loads(text), tomllib.loads(text)
    assert same(mini, real)
    assert harness_toml.preset_level(mini) == harness_toml.preset_level(real)
    assert harness_toml.comment_ratio(mini) == harness_toml.comment_ratio(real)


def test_the_reader_reads_scaffolds_file_and_the_defaults() -> None:
    rendered = core.render_harness_toml({"name": "demo", "goal": 'A "goal"', "notebook": "n.ipynb"})
    defaults = core.DEFAULTS_TOML.read_text(encoding="utf-8")
    for text in (rendered, defaults, rendered.replace("# level = ", "level = ")):
        assert same(tomlread._mini_loads(text), tomllib.loads(text))


def test_errors_name_the_line_never_the_value() -> None:
    secret = "Sup3rS3cret-" + "Passw0rd-2026"  # fake
    for text, message in (
        (f"x = 1\npassword = {secret}\n", "Invalid value for password (at line 2)"),
        (f'password = "{secret}\n', "Invalid value for password (at line 1)"),
        (f"a = 1\n{secret} {secret}\n", "Invalid statement (at line 2)"),
        (f'p = "{secret}"\np = "{secret}"\n', "Cannot overwrite a value (at line 2)"),
        ("[a]\nx = 1\n[a]\n", "Cannot declare a table twice (at line 3)"),
    ):
        with pytest.raises(ValueError) as caught:
            tomlread._mini_loads(text)
        assert str(caught.value) == message


def test_key_parts() -> None:
    assert tomlread.key_parts("a . \"b\\U0001F600\" . 'c' = 1") == (
        [("a", False), ("b\U0001f600", True), ("c", True)],
        24,
    )
    assert tomlread.key_parts('"\\x41" = 1') == ([(None, True)], 7)  # an escape TOML lacks
    assert tomlread.key_parts("[preset]", 1) == ([("preset", False)], 7)
    assert tomlread.key_parts("= 1") is None
    assert tomlread.key_parts('"open = 1') is None


def test_basic_string_escapes() -> None:
    assert tomlread.basic_string('a\\tb\\u00e9\\U0001F600\\\\\\"') == 'a\tbé\U0001f600\\"'
    for body in ("\\x41", "\\e", "\\u12", "\\uD800", "\\U00110000", 'a"b', "a\x01"):
        with pytest.raises(ValueError):
            tomlread.basic_string(body)
    assert tomlread.basic_string('a""', multiline=True) == 'a""'


# Generated documents: lines drawn from headers, keys and values that mix what the reader reads,
# what it refuses and what TOML forbids.
_KEYS = ["a", "b", "level", "preset", '"preset"', "'lint'", '"a.b"', "x-y", '""', '"\\u00e9"']
_VALUES = [
    "1", "+1", "0x1F", "01", "1__0", "1.5", "1e3", "1.", "inf", "-nan", "infinity", "true",
    "True", '"s"', "'s'", '"""s"""', "'''s'''", '"a\\"b"', '"a\\qb"', '"#x"', '"a" b', "[]",
    "[1,]", "[,]", "[[1],[2]]", "{}", "{a = 1}", "1979-05-27", "[", "'''",
]  # fmt: skip
_HEADERS = [
    "[a]",
    "[b]",
    "[a.b]",
    "[[a]]",
    "[[a.b]]",
    '["a"]',
    "[preset]",
    "[[preset]]",
    "[a",
    "[]",
]
_OTHER = ["", "# c", "a", "= 1", "a.b.c = 1", "b.c = 3", "x = 1 # c"]
_LINE = st.one_of(
    st.sampled_from(_HEADERS),
    st.sampled_from(_OTHER),
    st.builds(
        lambda key, dotted, value: f"{key}{'.' + dotted if dotted else ''} = {value}",
        st.sampled_from(_KEYS),
        st.one_of(st.none(), st.sampled_from(_KEYS)),
        st.sampled_from(_VALUES),
    ),
)


@settings(max_examples=400, deadline=None)
@given(st.lists(_LINE, min_size=1, max_size=7), st.sampled_from(["", "\n", "\r\n"]))
def test_generated_documents_read_as_tomllib_reads_them_or_are_refused(
    lines: list[str], end: str
) -> None:
    text = "\n".join(lines) + end
    assert agrees(text), text
