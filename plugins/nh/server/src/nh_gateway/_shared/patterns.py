"""Patterns shared by the Bash guard hook and the gateway linter."""

from __future__ import annotations

import re

_I = re.IGNORECASE

# Shell commands that would write a notebook or nh's own state behind the gateway's back.
SHELL_NOTEBOOK_WRITES = [
    # > >> >| >& &> into a notebook
    ("redirect", re.compile(r">{1,2}[|&]?\s*[\"']?[^\s\"'|;&]*\.ipynb\b", _I)),
    ("tee", re.compile(r"\btee\b[^|;&\n]*\.ipynb\b", _I)),
    (
        "in-place edit",
        re.compile(
            r"\b(?:sed|gsed|perl|ruby)\b[^|;&\n]*\s-[A-Za-z]*i[A-Za-z]*\b[^|;&\n]*\.ipynb\b", _I
        ),
    ),
    (
        "file operation",
        re.compile(r"\b(?:mv|cp|rm|truncate|touch|ln|install|rsync|dd)\b[^|;&\n]*\.ipynb\b", _I),
    ),
    (
        "git restore",
        re.compile(r"\bgit\s+(?:checkout|restore|reset|stash|apply)\b[^|;&\n]*\.ipynb\b", _I),
    ),
    (
        "nbconvert in place",
        re.compile(
            r"\bjupyter[- ](?:nbconvert|execute|trust)\b[^|;&\n]*(?:--inplace|--output[= ]\S*\.ipynb)",
            _I,
        ),
    ),
    ("jupytext", re.compile(r"\bjupytext\b", _I)),
    ("nbstripout", re.compile(r"\bnbstripout\b", _I)),
    ("papermill", re.compile(r"\bpapermill\b", _I)),
    (
        "nbqa fix",
        re.compile(r"\bnbqa\b[^|;&\n]*(?:--fix|black|isort|autopep8|pyupgrade|--nbqa-mutate)", _I),
    ),
    ("nbformat write", re.compile(r"nbformat\.writes?\b", _I)),
    (
        "python write",
        re.compile(
            r"(?:json\.dump|write_text|open\([^)]*['\"][wa+]).*\.ipynb|\.ipynb.*(?:json\.dump|write_text)",
            _I,
        ),
    ),
    (
        "python open",
        re.compile(r"open\([^)]*\.ipynb['\"]\s*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+]", _I),
    ),
    ("PowerShell write", re.compile(r"(?:Set-Content|Out-File|Add-Content)[^|;\n]*\.ipynb\b", _I)),
]

SHELL_NH_STATE_WRITE = re.compile(
    r"(?:>{1,2}[|&]?|\btee\b|\b(?:sed|perl)\b[^|;&\n]*\s-[A-Za-z]*i|\b(?:mv|cp|rm|truncate|touch)\b)"
    r"[^|;&\n]*(?:^|[\s/\"'=])\.nh(?:/|\b)",
    _I,
)

# Code inside a cell that writes notebooks (lint rule L008).
CELL_NOTEBOOK_WRITES = [
    re.compile(r"^\s*%%writefile\s+\S*\.ipynb\b", _I | re.MULTILINE),
    re.compile(r"nbformat\.writes?\s*\(", _I),
    re.compile(r"^\s*!.*\bjupyter[- ]nbconvert\b.*--inplace", _I | re.MULTILINE),
    re.compile(r"^\s*!.*\bjupytext\b", _I | re.MULTILINE),
    re.compile(r"(?:json\.dump|write_text|open\([^)]*['\"][wa+]).*\.ipynb", _I),
    re.compile(r"open\([^)]*\.ipynb['\"]\s*,\s*(?:mode\s*=\s*)?['\"][^'\"]*[wax+]", _I),
    re.compile(r"\.ipynb['\"]\s*\)\s*\.write_(?:text|bytes)\s*\(", _I),
]

# Package installs from inside a cell (lint rule L009).
CELL_PACKAGE_INSTALL = re.compile(
    r"^\s*(?:!|%)\s*(?:pip3?|conda|mamba|micromamba|uv)\b[^\n]*\b(?:install|add)\b"
    r"|^\s*%(?:pip|conda|mamba)\s+(?:install|add|uninstall|remove)\b"
    r"|^\s*[!%].*?(?:-m\s+pip|\b(?:pip3?|conda|mamba|micromamba|uv))\s+(?:install|add)\b"
    r"|\bos\.(?:system|popen)\([^)]*\b(?:pip3?|conda|mamba|uv)\b[^)]*\b(?:install|add)\b"
    r"|subprocess\.[a-z_]+\([^)]*\b(?:pip|conda|uv)\b[^)]*\b(?:install|add)\b",
    _I | re.MULTILINE,
)

CELL_SEPARATORS = [
    re.compile(r"^#\s*%%"),
    re.compile(r"^#\s*In\s*\[\s*\d*\s*\]\s*:?"),
    re.compile(r"^#\s*<(?:code|markdown)cell>", _I),
    re.compile(r"^#\s*COMMAND\s*-{5,}"),
]


def shell_notebook_write(command: str):
    """Return the name of the first matching rule, or None."""
    for name, pattern in SHELL_NOTEBOOK_WRITES:
        if pattern.search(command):
            return name
    if SHELL_NH_STATE_WRITE.search(command):
        return "write into .nh/"
    return None
