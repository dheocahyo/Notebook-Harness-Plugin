"""nhctl metrics summarize|sample on a fixture log and fixture notebooks."""

from __future__ import annotations

import json

import pytest


def ev(event, turn, ts, session="s1", **fields):
    return {"v": 1, "ts": ts, "event": event, "session_id": session, "turn_id": turn, **fields}


EVENTS = [
    ev("turn_open", "t1", 1000.0),
    ev("cell_added", "t1", 1001.0, cell_uid="nh-a", note_words=30, bullet_words=[8, 12], harness_ms=400),
    ev("turn_open", "t2", 1100.0),
    ev("cell_rejected", "t2", 1101.0, reason="second_cell"),
    ev("cell_added", "t2", 1102.0, cell_uid="nh-b", note_words=50, bullet_words=[30, 5, 9],
       ms={"total": 1900, "exec": 1200}),
    ev("cell_added", "t2", 1103.0, cell_uid="nh-b", note_words=51, bullet_words=[31], harness_ms=650),
    ev("turn_open", "t3", 1200.0),  # a question: no cell
    ev("cell_rejected", "t3", 1201.0, reason="notes"),
    ev("cell_rejected", "t3", 1202.0, reason="second_cell"),
    ev("turn_open", "t4", 1300.0),
    ev("cell_added", "t4", 1301.0, cell_uid="nh-c", note_words=20, bullet_words=[4, 4], harness_ms=300),
    ev("cell_undone", "t5", 1400.0, cell_uid="nh-c"),
    ev("cell_review", "t5", 1401.0, cell_uid="nh-a", unedited=True, deleted=False),
    ev("cell_review", "t5", 1402.0, cell_uid="nh-b", unedited=False, deleted=False),
    ev("cell_review", "t5", 1403.0, cell_uid="nh-c", unedited=True, deleted=False),
    ev("fresh_run", None, 1500.0, nb="notebooks/01_eda.ipynb", ok=True),
    ev("fresh_run", None, 1501.0, nb="notebooks/01_eda.ipynb", ok=False),
    ev("inspect", "t5", 1502.0, ms=40),
]  # fmt: skip


@pytest.fixture
def nh_project(project):
    (project / ".nh").mkdir()
    rotated, current = EVENTS[:6], EVENTS[6:]
    (project / ".nh/log.1.jsonl").write_text("".join(json.dumps(e) + "\n" for e in rotated))
    lines = [json.dumps(e) for e in current] + ['{"torn', "[1, 2]", '{"no_event": true}']
    (project / ".nh/log.jsonl").write_text("\n".join(lines) + "\n")
    return project


def test_summarize(env, nh_project):
    summary = env.json("metrics", "summarize", cwd=nh_project)
    assert summary["logs"] == [".nh/log.1.jsonl", ".nh/log.jsonl"]
    assert summary["turns"] == 4
    assert summary["cells_added"] == 3
    assert summary["cells_per_turn"] == {"0": 1, "1": 3}
    assert summary["max_cells_per_turn"] == 1
    assert summary["rejections"] == {"total": 3, "by_reason": {"second_cell": 2, "notes": 1}}
    # nh-c was undone, nh-b edited: only nh-a counts as accepted.
    assert summary["acceptance"] == {"reviewed": 3, "accepted": 1, "rate": 0.333, "unreviewed": 0}
    assert summary["undo"] == {"undone": 1, "rate": 0.333}
    assert summary["note_words"] == {"n": 3, "median": 30, "p90": 50, "max": 50}
    assert summary["bullet_words"] == {"n": 7, "median": 8, "p90": 30, "max": 30, "over_hint": 1}
    assert summary["fresh_run"] == {"runs": 2, "passed": 1, "rate": 0.5}
    assert summary["harness_ms"] == {"n": 4, "p50": 400, "p95": 700}


def test_summarize_since(env, nh_project):
    summary = env.json("metrics", "summarize", "--since", "1970-01-01T00:20:00Z", cwd=nh_project)
    assert summary["turns"] == 2  # t3 (a question) and t4, plus the late review/undo events
    assert summary["cells_added"] == 1
    assert summary["fresh_run"]["runs"] == 2
    bad = env.json("metrics", "summarize", "--since", "last tuesday", cwd=nh_project, expect=1)
    assert bad["error"]["code"] == "D180"


def test_rejections_count_each_rule_id(env, project):
    """``reason`` is a list of rule ids (design §5); older logs joined them with commas."""
    (project / ".nh").mkdir()
    events = [
        ev("cell_rejected", "t1", 1.0, reason=["L003", "L004"]),
        ev("cell_rejected", "t1", 2.0, reason="L003,L005"),
        ev("cell_rejected", "t1", 3.0, reason=["L003"]),
        ev("cell_rejected", "t1", 4.0, reason=[]),
        ev("cell_rejected", "t1", 5.0),
    ]
    (project / ".nh/log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    summary = env.json("metrics", "summarize", cwd=project)
    assert summary["rejections"] == {
        "total": 5, "by_reason": {"L003": 3, "L004": 1, "L005": 1, "unknown": 2},
    }  # fmt: skip
    text = env.run("metrics", "summarize", cwd=project).stdout
    assert "rejections: 5 (L003 3, L004 1, L005 1, unknown 2)" in text


def test_background_task_notifications_are_not_turns(env, project):
    (project / ".nh").mkdir()
    events = [
        ev("turn_open", "t1", 1000.0),
        ev("turn_alias", "t1", 1001.0, prompt_id="n1", prompt_chars=420, tasks=["w2g9v11xz"]),
        ev("cell_added", "t1", 1002.0, cell_uid="nh-a", note_words=20),  # the canonical turn
        ev("turn_alias", "t1", 1003.0, prompt_id="n2", prompt_chars=380, tasks=["b7"]),
        ev("turn_alias", None, 1004.0, prompt_id="n3", prompt_chars=390, tasks=["x1"]),  # orphan
    ]
    (project / ".nh/log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    summary = env.json("metrics", "summarize", cwd=project)
    assert summary["turns"] == 1
    assert summary["cells_per_turn"] == {"1": 1}
    assert summary["max_cells_per_turn"] == 1


def test_messages_typed_mid_turn_are_not_turns(env, project):
    """A message folded into the running turn logs turn_absorbed (design §6.1): no turn."""
    (project / ".nh").mkdir()
    events = [
        ev("turn_open", "t1", 1000.0, mode="ask", request={"batch": True, "n": 3}, answer=None),
        ev("turn_absorbed", "t1", 1001.0, prompt_id="t1", prompt_chars=3, answer="yes"),
        ev("turn_alias", "t1", 1002.0, prompt_id="n1", prompt_chars=420, tasks=["b7"]),
        ev("turn_absorbed", "t1", 1003.0, prompt_id="n1", prompt_chars=9, mode="explain"),
        ev("cell_added", "t1", 1004.0, cell_uid="nh-a", note_words=20),
        ev("turn_absorbed", None, 1005.0, prompt_id="o1", prompt_chars=12),  # an orphan's turn
        ev("turn_absorbed", "t9", 1006.0, prompt_id="t9", prompt_chars=4),  # open rotated out
    ]
    (project / ".nh/log.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    summary = env.json("metrics", "summarize", cwd=project)
    assert summary["turns"] == 1
    assert summary["cells_per_turn"] == {"1": 1}


def test_summarize_human_and_empty(env, project):
    (project / ".nh").mkdir()
    empty = env.run("metrics", "summarize", cwd=project)
    assert empty.returncode == 0 and "No nh activity" in empty.stdout
    (project / ".nh/log.jsonl").write_text(json.dumps(EVENTS[0]) + "\n")
    text = env.run("metrics", "summarize", cwd=project).stdout
    assert "cells per turn: 0 cell(s): 1" in text


def notebook(cells):
    return {"cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}


def code(source, nh=None, count=None):
    meta = {"nh": nh} if nh else {}
    return {"cell_type": "code", "id": "x", "metadata": meta, "source": source,
            "outputs": [], "execution_count": count}  # fmt: skip


def test_sample_returns_agent_code_only(env, nh_project):
    note = {"cell_type": "markdown", "id": "n", "metadata": {"nh": {"role": "note"}},
            "source": "### Secret title\n\n- a bullet"}  # fmt: skip
    cells = [
        note,
        code(["df = load()\n", "df.head()"], {"role": "code", "uid": "nh-1"}, 3),
        code("human_code = 1", None, 4),
        code("   ", {"role": "code", "uid": "nh-2"}, 5),
        code("plot(df)", {"role": "code", "uid": "nh-3"}, None),
    ]
    (nh_project / "notebooks").mkdir()
    (nh_project / "notebooks/a.ipynb").write_text(json.dumps(notebook(cells)))
    (nh_project / ".nh/hidden.ipynb").write_text(json.dumps(notebook(cells)))
    result = env.json("metrics", "sample", "--n", "5", "--seed", "1", cwd=nh_project)
    assert result["pool"] == 2
    got = sorted(result["cells"], key=lambda cell: cell["code"])
    assert got == [
        {"notebook": "notebooks/a.ipynb", "exec": 3, "code": "df = load()\ndf.head()"},
        {"notebook": "notebooks/a.ipynb", "exec": None, "code": "plot(df)"},
    ]
    text = env.run("metrics", "sample", "--n", "1", "--seed", "1", cwd=nh_project).stdout
    assert text.startswith("--- notebooks/a.ipynb")
    assert "Secret title" not in text and "bullet" not in text and "human_code" not in text
