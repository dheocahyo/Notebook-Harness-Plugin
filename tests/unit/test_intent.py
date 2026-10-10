"""The message classifier (_shared/intent.py, design §6.1): mode, request and answer."""

from __future__ import annotations

from typing import Any

import pytest

from nh_gateway._shared import intent

EXPLAIN = {"mode": "explain", "request": None, "answer": None}
PLAN = {"mode": "plan", "request": None, "answer": None}
NONE = {"mode": None, "request": None, "answer": None}
YES = {"mode": None, "request": None, "answer": "yes"}
NO = {"mode": None, "request": None, "answer": "no"}
RERUN = {"mode": "ask", "request": {"rerun_stale": True}, "answer": None}


def batch(n: int) -> dict[str, Any]:
    return {"mode": "ask", "request": {"batch": True, "n": n}, "answer": None}


TABLE: list[tuple[str, dict[str, Any]]] = [
    # explain: the message starts with "explain" (or /nh:explain) and asks for no change
    ("explain the fixed-width parse", EXPLAIN),
    ("Explain the fixed-width parse", EXPLAIN),
    ("explain", EXPLAIN),
    ("explain.", EXPLAIN),
    ("  explain why the join drops rows  ", EXPLAIN),
    ("explain: what does cell 4 do?", EXPLAIN),
    ("/nh:explain the outlier filter", EXPLAIN),
    ("explain the fixed columns", EXPLAIN),  # "fixed" is not the verb "fix"
    ("explain what changes if we drop nulls", EXPLAIN),  # nor is "changes"
    ("explain make_features", EXPLAIN),  # "-" and "_" join words: not the verb "make"
    ("explain the add-on config", EXPLAIN),
    ("explain the make-up of df", EXPLAIN),
    ("explain the fix", NONE),  # a base form, even as a noun
    ("explain and fix", NONE),
    ("explain the bug, then fix it", NONE),
    ("explain and change the threshold", NONE),
    ("explain it and add a test cell", NONE),
    ("explain then update the plot", NONE),
    ("explain, rewrite it shorter", NONE),
    ("explain and refactor", NONE),
    ("explain and make it faster", NONE),
    ("explainer for the model", NONE),
    ("explain-like-i'm-five the merge", NONE),
    ("please explain the merge", NONE),  # only a message that starts with it
    ("can you explain the merge?", NONE),
    # lead marks before the first word are skipped for explain (design §6.1): a miss fails open
    ('"explain cell 3"', EXPLAIN),
    ("'explain cell 3'", EXPLAIN),
    ("“explain cell 3”", EXPLAIN),
    ("`explain` cell 3", EXPLAIN),
    ("**Explain** cell 3", EXPLAIN),
    ("*explain*", EXPLAIN),
    ("> explain cell 3", EXPLAIN),
    ("(explain cell 3)", EXPLAIN),
    ("[explain] cell 3", EXPLAIN),
    ("- explain", EXPLAIN),
    ("#explain", EXPLAIN),
    ("```\nexplain cell 3\n```", EXPLAIN),
    ("﻿explain cell 3", EXPLAIN),  # a BOM
    ("​explain cell 3", EXPLAIN),  # a zero-width space
    (' "/nh:explain" 3', EXPLAIN),
    ("`explain` is null in 30% of rows; drop those rows", EXPLAIN),  # a false hit fails safe
    ('"explain and fix it"', NONE),  # the change verb still counts
    ("1. explain", NONE),  # a digit starts a word
    ("_explain_ cell 3", NONE),  # so does "_"
    ('"/nh:plan" run next 3', batch(3)),  # plan is the typed command only
    # plan
    ("/nh:plan", PLAN),
    ("/nh:plan clean the sales data", PLAN),
    ("/NH:PLAN build the model", PLAN),
    ("/nh:plan run the next 3", PLAN),  # a plan carries no request
    ("/nh:plan explain the steps", PLAN),
    ("/nh:planning ahead", NONE),
    ("/nh:plan-b", NONE),
    ("let's /nh:plan this", NONE),
    # answers: the whole message
    ("go", YES),
    ("Go!", YES),
    ("yes", YES),
    ("Yes.", YES),
    ("y", YES),
    ("yep", YES),
    ("yeah", YES),
    ("yup", YES),
    ("sure", YES),
    ("ok", YES),
    ("OK!", YES),
    ("okay", YES),
    ("go ahead", YES),
    ("go  ahead", YES),
    ("do it", YES),
    ("please do", YES),
    ("yes, please", YES),
    ("sounds good", YES),
    ("approved", YES),
    ("yes…", YES),
    ("ok !", YES),
    ("no", NO),
    ("No.", NO),
    ("n", NO),
    ("nope", NO),
    ("nah", NO),
    ("no, thanks", NO),
    ("don't", NO),
    ("don’t", NO),
    ("dont", NO),
    ("do not", NO),
    ("not now", NO),
    ("cancel", NO),
    ("stop", NO),
    ("skip", NO),
    ("skip it", NO),
    ("go on", NONE),
    # a question isn't consent
    ("yes?", NONE),
    ("ok?", NONE),
    ("go?", NONE),
    ("sure?", NONE),
    ("yes!?", NONE),
    ("no?", NONE),
    ("go for step 2", NONE),
    ("yes, but drop the nulls", NONE),
    ("no, use the median instead", NONE),
    ("yesterday's file", NONE),
    ("nothing", NONE),
    ("okay so what next", NONE),
    ("next", NONE),
    ("", NONE),
    ("   ", NONE),
    # batch: "run the next N" (N >= 2) or "run steps A-B"
    ("run the next 3", batch(3)),
    ("run next 3", batch(3)),
    ("Run the next 2 steps", batch(2)),
    ("run the next three", batch(3)),
    ("run the next ten steps", batch(10)),
    ("please run the next 4 cells", batch(4)),
    ("ok, run the next 5", batch(5)),
    ("run steps 3-5", batch(3)),
    ("run steps 3 - 5", batch(3)),
    ("run steps 3–5", batch(3)),
    ("run step 1-2", batch(2)),
    ("run steps 2-11", batch(10)),
    ("run the next 999", batch(999)),
    ("run steps 1-999", batch(999)),
    ("run next 1 then run steps 3-5", batch(3)),  # the first phrase that is a request
    ("run the next 1000", NONE),  # counts are 1-3 digits and n <= 999
    ("run steps 0-999", NONE),
    ("run steps 1000-1002", NONE),
    ("run next 1", NONE),
    ("run the next one", NONE),
    ("run the next step", NONE),
    ("run steps 5-5", NONE),
    ("run steps 5-3", NONE),
    ("rerun the next 3", NONE),
    ("run the next 3x", NONE),
    ("the next 3 look good", NONE),
    # a negated or past-tense phrasing is no request
    ("don't run the next 3", NONE),
    ("don’t run the next 3", NONE),
    ("do not run the next 3", NONE),
    ("never run steps 2-4", NONE),
    ("please don't ever run the next 3", NONE),
    ("no need to run the next 3", NONE),
    ("why did you run the next 3?", NONE),
    ("did you run steps 2-4?", NONE),
    ("you shouldn't run the next 3", NONE),
    ("no, run the next 3", batch(3)),  # the comma ends the "no"
    ("you have to run the next 3", batch(3)),
    ("don't run the next 3; run the next 2 instead", batch(2)),
    # re-run stale
    ("re-run the stale cells", RERUN),
    ("rerun the stale cells", RERUN),
    ("Re-run stale", RERUN),
    ("re-run all the stale cells", RERUN),
    ("please rerun all stale cells", RERUN),
    ("re-run the cells that are stale", NONE),
    ("run the stale cells", NONE),
    ("prerun stale", NONE),
    ("don't re-run the stale cells", NONE),
    ("never rerun stale cells", NONE),
    # batch wins over re-run stale; explain and plan win over both
    ("run the next 2 and re-run the stale cells", batch(2)),
    ("explain before you run the next 3", EXPLAIN),
    ("explain why re-run the stale cells", EXPLAIN),
]


def test_the_table_is_large_enough() -> None:
    assert len(TABLE) >= 60


@pytest.mark.parametrize(("text", "expected"), TABLE)
def test_classify(text: str, expected: dict[str, Any]) -> None:
    assert intent.classify(text) == expected


@pytest.mark.parametrize("value", [None, 3, b"yes", ["go"], {"prompt": "go"}])
def test_non_text_is_an_ordinary_message(value: Any) -> None:
    assert intent.classify(value) == NONE


def test_a_long_message_is_never_an_answer() -> None:
    # Padded inside the text: trimming can't shorten it.
    at_limit = "yes" + " " * (intent.ANSWER_MAX_CHARS - 4) + "."
    over = "yes" + " " * (intent.ANSWER_MAX_CHARS - 3) + "."
    assert (len(at_limit), len(over)) == (intent.ANSWER_MAX_CHARS, intent.ANSWER_MAX_CHARS + 1)
    assert intent.classify(at_limit)["answer"] == "yes"
    assert intent.classify(over)["answer"] is None


@pytest.mark.parametrize(
    "text", ["run the next " + "9" * 4400, "run steps 1-" + "9" * 4400, "run the next " + "9" * 26]
)
def test_a_huge_count_is_no_request(text: str) -> None:
    """Python 3.9 would store it and 3.13 couldn't read the record back (or would raise)."""
    assert intent.classify(text) == NONE


def test_the_answer_sets_are_disjoint_and_normalised() -> None:
    assert not intent.YES & intent.NO
    for word in intent.YES | intent.NO:
        assert word == word.strip().lower()
        assert intent.classify(word)["answer"] == ("yes" if word in intent.YES else "no")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"batch": True, "n": 3}, {"batch": True, "n": 3}),
        ({"rerun_stale": True}, {"rerun_stale": True}),
        ({"batch": True, "n": 1}, None),
        ({"batch": True, "n": 999}, {"batch": True, "n": 999}),
        ({"batch": True, "n": 1000}, None),
        ({"batch": True, "n": 10**30}, None),
        ({"batch": True, "n": True}, None),
        ({"batch": True, "n": 3.0}, None),
        ({"batch": True, "n": "3"}, None),
        ({"batch": 1, "n": 3}, None),
        ({"batch": True, "n": 3, "extra": 1}, None),
        ({"rerun_stale": False}, None),
        ({"rerun_stale": True, "n": 2}, None),
        ({}, None),
        ([], None),
        ("batch", None),
        (None, None),
    ],
)
def test_valid_request(value: Any, expected: Any) -> None:
    assert intent.valid_request(value) == expected
