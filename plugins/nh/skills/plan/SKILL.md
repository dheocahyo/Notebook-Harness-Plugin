---
name: plan
description: >-
  Plan a goal for this Notebook Harness project's notebook in chat, as 5-12
  numbered steps of one cell each, and change nothing. Runs when the user
  types /nh:plan with a goal; "go" then writes step 1, and "run the next N"
  writes several steps in one reply after one question.
argument-hint: "<goal>"
disable-model-invocation: true
allowed-tools:
  - mcp__plugin_nh_nh__nh_inspect
---

# /nh:plan: plan in chat, write nothing

This message is plan-only: no `nh_add_cell`, `nh_edit_cell`, `nh_run` or
`nh_undo` (nh refuses them: E109), no code and no code fence. The plan lives
in chat, never in the notebook.

1. Look first: `nh_inspect(view="outline")` for the cells that exist, and
   `nh_inspect(view="vars")` for the data and what is installed. Never Read
   the `.ipynb` file.
2. Reply with the plan, in the format of
   [planning.md](../notebook/reference/planning.md):
   - 5-12 numbered steps toward the user's goal. Each is one cell with one
     visible output the user can check (a table, a shape, one plot).
   - Start each step with its title in bold (a verb, at most 8 words), then
     a colon and the one result it shows, in a few words: "**Drop customers
     without a signup date**: rows before and after." One result, not two
     joined by "and": "the dates that fail to convert", not "the date type,
     and the dates that fail to convert". A decision the user will face is a
     second short sentence: "**Handle the missing ages**: the rows without an
     age. You choose whether to drop, fill or keep them." One action per
     step: a step that would do two things ("list the names, then merge
     them") is two steps.
   - Plain words only: no code, commands, constants, method or variable
     names, and no backticks. Say "the first rows", not the method.
   - Start from what the user asked: the reply opens with the plan and says
     nothing about what the notebook holds, before the list or after it
     ("cell [1] already loads the data" and "I left out the loading step" are
     recaps: leave them out). A step the notebook already holds (the loader,
     a check that ran) is not listed.
   - A package that isn't installed yet: say so in words at the step that
     needs it (when that step comes, ask the user before installing it). A
     package that `nh_inspect` lists under "installed:", "(not imported)" or
     not, gets no word: "**Plot signups per week**: one line chart.", not
     "… one line chart. This uses matplotlib, which is installed."
3. End with one question, in these words: "Where should I start? Say "go"
   for step 1, or "run the next 3" to do several in one reply." (or another
   number, or "run steps a-b"; you then ask once before writing them).

Later messages:
- An edit of the plan ("drop step 4", "swap steps 3 and 4", "add a step that
  plots by month"): re-print the whole updated list and write nothing.
- "go": write the step the last reply proposed (step 1 right after the plan)
  as that message's one cell.
- "run the next 3" or "run steps 2-4", then the user's yes: follow "The
  batch path" in [planning.md](../notebook/reference/planning.md).

Name cells by title and `[n]`; never mention `nh-` ids.
