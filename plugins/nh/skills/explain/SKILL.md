---
name: explain
description: >-
  Explain a notebook cell of this Notebook Harness project step by step in
  chat, with the real values from its output, and change nothing. Runs when
  the user types /nh:explain, optionally naming the cell (by title or [n]);
  without one it explains the last cell.
argument-hint: "[cell title or [n]] [question]"
disable-model-invocation: true
allowed-tools:
  - mcp__plugin_nh_nh__nh_inspect
---

# /nh:explain: walk through a cell, change nothing

This message is explain-only: no `nh_add_cell`, `nh_edit_cell`, `nh_run` or
`nh_undo`, and the explanation goes in chat, never in the notebook. nh refuses
those calls (E109) when the message names no change (fix, change, add,
update, rewrite, refactor, make, even as a noun); the rule holds either way.

1. Find the cell: call `nh_inspect(view="outline")` and pick the cell the
   user named by title or `[n]` (a bare number means `[n]`, the execution
   count, not the outline's row number). With no name, take the cell the
   user's question is about, if any; else the last cell the nh reminder names
   (a title it cut, ending in …, matches by prefix) or, if it names none, the
   last code cell nh wrote (author `agent` or `agent*` in the outline), else
   the last code cell that ran. If two cells match, ask which one.
2. Read it: `nh_inspect(view="cell", cell_id=...)` gives its code, its note
   and its outputs. Call `nh_inspect(view="var", name=...)` only when a value
   the walkthrough needs is not in the outputs.
3. Reply with a numbered walkthrough, one group of lines at a time:
   - quote the lines (a short code block);
   - say what they do and why, in plain words;
   - give the real values from the output (row counts, columns, nulls,
     numbers). Never invent or round away a value; if the output doesn't
     show one, say so.
4. After the steps, say in one or two sentences what the cell leaves behind
   (the names it creates and what they hold) and any judgment call in it,
   naming its UPPER_CASE constants.
5. End by offering the next step: a follow-up question, or one proposed next
   cell as a title the user can approve with "go". Don't write it.

Depth: plain and junior-level, defining each pandas method the first time it
appears (one short clause each), unless nh's session context sets another
depth: then follow it, and still walk through every part in numbered steps. If
the user asked a question, answer it first, then walk through the cell.

Never:
- write the explanation into the notebook (no note, no comment, no markdown
  cell);
- change, re-run or undo any cell, even to show a value;
- Read the `.ipynb` file: `nh_inspect` is the only way in.

Name cells by title and `[n]`; never mention `nh-` ids or line numbers.
