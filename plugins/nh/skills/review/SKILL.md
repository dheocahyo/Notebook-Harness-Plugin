---
name: review
description: >-
  Review this Notebook Harness project's notebook: run a copy of it top to
  bottom in a separate kernel and report failing cells, hidden-state
  dependencies, cells long enough to move to src/, and a one-page intent
  summary. Never touches the live kernel or the notebook. Runs when the user
  types /nh:review, optionally naming a notebook.
argument-hint: "[notebook]"
disable-model-invocation: true
allowed-tools:
  - Bash(nhctl fresh-run --review *)
---

# /nh:review: run a copy of the notebook and report

The review runs a copy of the notebook top to bottom in a separate kernel,
past errors; the notebook, its kernel and their state stay as they are. Call
no nh tool: `nh_inspect` would probe the live kernel, and this message
changes no cell.

1. Say first, in one line, that the review can take a few minutes: it runs
   every cell again in a separate kernel, and the notebook and its kernel stay
   as they are.
2. Run this with Bash, with `timeout: 600000`:

   ```
   nhctl fresh-run --review --plugin-data "${CLAUDE_PLUGIN_DATA}" --timeout 540 --json
   ```

   If the user named a notebook, put its path right after `--review`. Add no
   `--cell-timeout`: the review's own limit (180 s here) interrupts a cell that
   hangs and goes on. If the shell can't find `nhctl`, say nh's `bin/` isn't
   on PATH (the plugin is disabled or needs a restart) and stop.

   A `yes` or `no` after `/nh:review` names no notebook: it answers step 3's
   question, so run step 3's rerun instead (with no D154 above, this command
   as it is).
3. Exit 2, the JSON's `error.code` `D154`: some cells would do more than
   compute, and nothing ran. Show this, with one line per cell of the JSON's
   `flagged` list, then stop and wait for the answer (one question; no other
   call):

   > The review runs every cell again in a separate kernel. These cells would also do more there:
   > - <label>: <what>
   >
   > Run them in the review too? Type `/nh:review yes` to run every cell, or `/nh:review no` to skip these; the report then lists them as not run.

   `<label>` is the cell's `label`, as given. `<what>` is its `rules`, each in
   these words, joined with "; ":

   | Rule | Words |
   |---|---|
   | `package_install` | installs packages |
   | `network` | reaches the network |
   | `outside_write` | writes outside the project |
   | `secret_print` | shows environment variables |
   | `secret_name` | shows a value named like a secret |
   | `unreadable` | is code nh can't parse, so it can't tell what it does |

   The rerun: step 2's command with, right after `--review`, `--yes <digest>`
   for a yes (the JSON's `digest`, as given) or `--skip-flagged` for a no, then
   the JSON's `notebook` in double quotes:
   `nhctl fresh-run --review --yes <digest> "<notebook>" --plugin-data …`.
   `/nh:review yes` or `no` runs it without a permission prompt; a plain yes or
   no gets the same rerun, and Claude Code asks the user to allow it. A D154
   again after a yes means the flagged cells changed meanwhile: ask again, the
   same way, about the new list.
4. Exit 0, or exit 1 with `error.code` `D153`: show the report from the JSON.
   With D153, say first that the review stopped before the end (`error`) and
   at which cell (`stopped_at`), so the cells after it didn't run. A longer
   review runs from a shell, `nhctl fresh-run --review --timeout 1800` (it
   writes the same report): say so, and don't rerun it here with a larger
   `--timeout` (Bash stops a command after 600 s). Then:
   - failing cells (`failing`): each label with its error (`ename`: `evalue`);
   - hidden state (`hidden_state`): names read before any cell above defines
     them, with the cell that defines each (`defined_in`); cells that ran out
     of order, each as "<label> ran before <its `after` cell's label> above
     it" (its count is the lower one) or "<label> has the same count as <its
     `same_as` cell's label> above it, so they ran in different kernel
     sessions"; cells that fail only in a fresh kernel, each as "<label>:
     <ename> here, though the notebook shows it ran without an error", plus
     "(a cell above it failed in the review)" with `after_error`, "(a cell
     above it didn't run in the review)" with `after_not_run`, or "(cells
     above it failed or didn't run in the review)" with both;
   - cells to move to `src/` (`src_candidates`), each with its line count:
     the cells over `max_cell_lines` lines, the project's limit (no cell's
     length); with none, say no cell is over that limit;
   - cells not run (`not_run`) and why;
   - the intent summary (`summary`): each heading and its cells' intents.

   An empty part gets one short line. End by naming the report file
   (`report`). If Claude Code saved the output to a file instead of showing
   it all ("Output too large"), Read that file: it holds the whole JSON. Any
   other error: its `message` and `fix`.

Name cells by their labels only; never mention `nh-` ids, line numbers or the
review's copy of the notebook.
