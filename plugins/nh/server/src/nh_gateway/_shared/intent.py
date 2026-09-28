"""Classify one human message for the turn record (design §6.1): mode, request and answer.

``classify(text) -> {"mode", "request", "answer"}``:
- ``mode``: ``explain`` | ``plan`` | ``ask`` | None
- ``request``: ``{"batch": True, "n": n}`` (2 <= n <= 999) | ``{"rerun_stale": True}`` | None;
  a negated phrasing ("don't run the next 3") is no request
- ``answer``: ``yes`` | ``no`` | None, only when the whole message is a yes or a no (a
  message ending in "?" is neither)

Pure and regex only: whole words, case-insensitive. The prompt text is never stored; only
these fields reach the record and the log.

Stdlib only and Python 3.9 compatible: hooks may run on the system Python.
"""

from __future__ import annotations

import re
from typing import Any

MODES = ("explain", "plan", "ask")
ANSWERS = ("yes", "no")

YES = frozenset(
    {
        "yes",
        "y",
        "yep",
        "yeah",
        "yup",
        "sure",
        "ok",
        "okay",
        "go",
        "go ahead",
        "do it",
        "please do",
        "yes please",
        "sounds good",
        "approved",
    }
)
NO = frozenset(
    {
        "no",
        "n",
        "nope",
        "nah",
        "no thanks",
        "don't",
        "dont",
        "do not",
        "not now",
        "cancel",
        "stop",
        "skip",
        "skip it",
    }
)
# Answers are short: longer messages skip the normalisation.
ANSWER_MAX_CHARS = 40
# A batch is 2..999 steps: counts are 1-3 digits, so no hook can write an n that Python 3.13's
# json module refuses to read back (over 4300 digits).
MIN_BATCH_N, MAX_BATCH_N = 2, 999

NUMBER_WORDS = {
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_WS = re.compile(r"\s+")
# "?" is not dropped: "yes?" questions the offer, it doesn't accept it.
_TRAILING = re.compile(r"[\s.!,;:…]+$")
_PLAN = re.compile(r"/nh:plan(?![\w-])")
_EXPLAIN = re.compile(r"(?:/nh:explain|explain)(?![\w-])")
# Base forms only, as whole words ("-" and "_" join words): "fixed", "changes", "makes",
# "add-on" and "make_features" are not change verbs.
_CHANGE = re.compile(r"(?<![\w-])(?:fix|change|add|update|rewrite|refactor|make)(?![\w-])")
# "run", but not the "run" of "re-run" (that is an existing cell, not the next steps).
_RUN = r"(?<![\w-])run\s+"
_COUNT = r"(\d{1,3})(?!\w)"
_BATCH_NEXT = re.compile(
    _RUN + r"(?:the\s+)?next\s+(?:" + _COUNT + "|(" + "|".join(NUMBER_WORDS) + r")\b)"
)
_BATCH_RANGE = re.compile(_RUN + r"steps?\s+(\d{1,3})\s*[-–]\s*" + _COUNT)
_RERUN_STALE = re.compile(r"(?<![\w-])re-?run\s+(?:all\s+)?(?:the\s+)?stale\b")
# The words before a "run" that make it no request, in the same clause, with at most one
# word between: "don't run", "did you run", "never run", "no need to run".
_NEGATED = re.compile(
    r"(?:\b(?:not|never|don't|dont|didn't|didnt|doesn't|doesnt|won't|wont|shouldn't|shouldnt"
    r"|can't|cant|cannot|did)(?:\s+[\w']+)?|\bno\s+need\s+to)\s+$"
)


def empty() -> dict[str, Any]:
    return {"mode": None, "request": None, "answer": None}


def classify(text: Any) -> dict[str, Any]:
    """The message's mode, request and answer (all None for an ordinary ask)."""
    result = empty()
    if not isinstance(text, str):
        return result
    message = text.strip().lower().replace("\u2019", "'")
    if len(message) <= ANSWER_MAX_CHARS:
        bare = _TRAILING.sub("", _WS.sub(" ", message.replace(",", " ")))
        result["answer"] = "yes" if bare in YES else "no" if bare in NO else None
    if _PLAN.match(message):
        result["mode"] = "plan"
    elif _EXPLAIN.match(message) and not _CHANGE.search(message):
        result["mode"] = "explain"
    else:
        request = _request(message)
        if request is not None:
            result["mode"], result["request"] = "ask", request
    return result


def _request(message: str) -> dict[str, Any] | None:
    """The first phrase that isn't negated: "run (the) next N", then "run steps a-b", then
    "re-run (all) (the) stale"."""
    for match in _BATCH_NEXT.finditer(message):
        digits, word = match.groups()
        n = int(digits) if digits else NUMBER_WORDS[word]
        if MIN_BATCH_N <= n <= MAX_BATCH_N and not _negated(message, match):
            return {"batch": True, "n": n}
    for match in _BATCH_RANGE.finditer(message):
        n = int(match.group(2)) - int(match.group(1)) + 1
        if MIN_BATCH_N <= n <= MAX_BATCH_N and not _negated(message, match):
            return {"batch": True, "n": n}
    for match in _RERUN_STALE.finditer(message):
        if not _negated(message, match):
            return {"rerun_stale": True}
    return None


def _negated(message: str, match: re.Match[str]) -> bool:
    return _NEGATED.search(message, 0, match.start()) is not None


def valid_request(value: Any) -> dict[str, Any] | None:
    """``value`` rebuilt as one of the two request shapes, or None."""
    if not isinstance(value, dict):
        return None
    if set(value) == {"batch", "n"} and value["batch"] is True:
        n = value["n"]
        if isinstance(n, int) and not isinstance(n, bool) and MIN_BATCH_N <= n <= MAX_BATCH_N:
            return {"batch": True, "n": n}
    if set(value) == {"rerun_stale"} and value["rerun_stale"] is True:
        return {"rerun_stale": True}
    return None
