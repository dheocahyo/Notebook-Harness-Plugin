---
name: init
description: >-
  Set up a Notebook Harness data-science project in the current folder: checks
  the setup, asks the goal, data and problem type, scaffolds folders,
  harness.toml, NOTEBOOK.md and an environment file, builds the project
  environment, starts JupyterLab and adds the first data-loading cell. Can also
  adopt an existing notebook.
argument-hint: "[goal] [data path or URL] | adopt <notebook.ipynb>"
disable-model-invocation: true
allowed-tools:
  - Bash(nhctl doctor *)
  - Bash(nhctl scaffold *)
  - Bash(nhctl env *)
  - Bash(nhctl lab *)
  - Bash(nhctl runtime *)
  - mcp__plugin_nh_nh__nh_inspect
  - mcp__plugin_nh_nh__nh_add_cell
---

# /nh:init: from a folder to a loaded dataframe

Arguments (may be empty): $ARGUMENTS

Run every step in this one turn and print one short status line per step.
Every `nhctl` command takes `--json`; read its JSON. `nhctl` is on the Bash
PATH while nh is enabled; if the shell can't find it, use
`sh "${CLAUDE_PLUGIN_ROOT}/bin/nhctl"` instead (Claude Code then asks the user
to approve each call). Always pass `--plugin-data "${CLAUDE_PLUGIN_DATA}"` to
`nhctl runtime`.

## 1. Check the setup
1. Run `nhctl doctor --json`. For every problem with `"blocking": true`, show
   its `message` and `fix`, then stop.
2. Run `nhctl runtime sync --background --plugin-data "${CLAUDE_PLUGIN_DATA}" --json`.
3. Check that `mcp__plugin_nh_nh__nh_inspect` is loaded or found by ToolSearch
   (`select:mcp__plugin_nh_nh__nh_inspect`). If not, tell the user now, before
   the questions: "nh's notebook tools aren't connected yet. Run /mcp, pick
   plugin:nh:nh and Reconnect (or restart Claude Code) while I set up the
   project." Then continue.
4. If `project.nh_enabled` is true, say the project is already set up; the
   steps below only fill in what is missing.

## 2. Ask, in ONE AskUserQuestion call
Prefill from the arguments and the doctor's `project` data. The user can always
answer in free text.
1. **Goal** (optional): "What should this analysis answer?" Offer up to 2
   guesses from the arguments or file names. No answer means "Explore <file>".
2. **Data**: "Where is the data?" From `project.data_candidates`, offer the
   best file twice, as "<path>: copy into data/raw" and "<path>: use in place"
   (list "use in place" first when the file is over 100 MB), plus up to 2
   other candidates. Free text takes any path, or an http(s) or database URL.
3. **Problem type**: eda (explore only), classification, regression,
   forecasting. Free text: clustering or other.
4. Only when the project already has `.ipynb` files (`project.notebooks`):
   **Notebook**: "Adopt an existing notebook?" Offer up to 3 of them and
   "No, start notebooks/01_eda.ipynb".

Ask nothing else here.

## 3. Scaffold
Run:
`nhctl scaffold --goal "<goal>" --data "<path or URL>" --data-mode <copy|in-place|auto> --problem-type <type> --json`
adding `--adopt <notebook>` when the user adopts one. It never overwrites a
file. Report what it created and what it kept, one line each.
- A URL with credentials is stored in `.env` as `DATA_URL`. Never repeat its
  query string, credentials or the path parts the report shows as `…` in chat.
- When the report's `data.approved_host` is set (an http(s), s3 or similar
  data URL; never a database URL, `file://` or localhost), scaffold approved
  that host in `.nh/state/approved_hosts.json`, so nh writes the loader cell
  without asking. Say so in the report.
- If `env.dev_diff` is not empty (an existing pyproject.toml or
  environment.yml lacks JupyterLab, jupyter-collaboration or ipykernel), show
  the diff and ask yes/no with AskUserQuestion. On yes, re-run the same
  command with `--add-dev-deps`. On no, stop: nh needs those packages.

## 4. Offer the deny rule
Ask yes/no with AskUserQuestion, showing the change to `.claude/settings.json`:
```diff
+ "permissions": { "deny": ["Edit(/**/*.ipynb)", "Edit(/.nh/**)"] }
```
Say what it does: raw notebook edits are hard-blocked, so every change goes
through nh's reviewed cells; existing settings are kept; the file is shared
with teammates. Only on yes, run `nhctl settings apply --yes --json` (Claude
Code asks the user to approve it). On no, move on.

## 5. Build the environment
1. Run `nhctl env sync --background --json`. With conda, warn that this takes
   3-10 minutes.
2. While it runs, explain how working with nh goes, in a few short lines:
   - Each message gets one cell: nh writes it with a short note above, runs it
     in their kernel and reports the real output.
   - They review it with **go** (do the proposed next step), **edit ...**,
     **undo** or **explain**.
   - To reject a cell, say undo or delete it in JupyterLab. Ctrl+Z only undoes
     their own typing.
   - Settings live in `harness.toml` at the project root.
3. Run `nhctl env wait --timeout 540 --json` with the Bash tool's `timeout`
   set to 600000. Repeat while `status` is `running`. On `error`, show the last
   lines and the fix, then stop.

## 6. Start JupyterLab
Run `nhctl lab start --json` and give the user the URL it reports (it never
contains the token). `status` `started`, `running` and `adopted` all mean
JupyterLab is up. `adopted` is one the user started themselves that serves
this project: nh uses it instead of starting a second one; say so. Never start
any other JupyterLab.

## 7. Add the loader cell (this turn's one cell)
Skip this step in adopt mode when the scaffold reports `adopt.has_loader`.
1. If the nh tools are deferred, load them with ToolSearch:
   `select:mcp__plugin_nh_nh__nh_inspect,mcp__plugin_nh_nh__nh_add_cell,mcp__plugin_nh_nh__nh_edit_cell`
   If they are still unavailable, stop and ask the user to run /mcp, pick
   plugin:nh:nh, Reconnect, then say "load the data".
2. Call `nh_inspect(view="status")`. Its hooks line says there is no turn
   record yet: that is expected in this /nh:init message (it started before
   `.nh/` existed, so nh's hooks skipped it). Don't report it as a problem;
   the hooks stamp every message from the next one on.
3. Call `nh_add_cell` with:
   - `title`: "Load raw data and check schema"
   - `notes`: 2 bullets: which reader and why (the file type), and that the
     schema table shows each column's dtype, non-null count, null % and
     number of unique values.
   - `intent`: "Load <file> and check its columns"
   - `code`: per [reference/first-cell.md](reference/first-cell.md), with
     `DATA_PATH` set to the scaffold's `data.path_from_notebook`.
4. If it fails (encoding, delimiter, sheet, credentials), fix the same cell
   with `nh_edit_cell`, at most twice; then explain plainly and stop.

## 8. Summary
Reply with:
- what was created, one line per top-level item
  ([reference/layout.md](reference/layout.md));
- the env manager, where the env lives, and the JupyterLab URL;
- the data's shape and 2-3 facts from the schema table (null-heavy, id-like or
  wrongly typed columns), with the real numbers;
- one suggested next cell, as a title they can approve with "go";
- how the loop works, in two lines, and that `/nh:status` checks the setup.
