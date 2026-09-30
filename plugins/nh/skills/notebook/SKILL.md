---
name: notebook
description: >-
  Pair with a data scientist in their live JupyterLab notebook, one reviewed
  code cell per user message (Notebook Harness). Use for any request that
  touches the notebook, .ipynb files, cells, the kernel, dataframes, pandas,
  plots, cleaning, EDA or models in a project with harness.toml or a .nh/
  folder, and for replies about the last cell: go, accept, edit, undo,
  explain, tidy, retry.
allowed-tools:
  - mcp__plugin_nh_nh__nh_inspect
---

# Notebook Harness: one cell per message

You work in the user's live JupyterLab notebook. They watch each cell appear,
read it, and must be able to own it. The nh tools enforce the rules below;
when a tool refuses, follow its `Next:` line.

**WARNING. The running kernel and the live JupyterLab document are the source
of truth, not the `.ipynb` file.** Never Read, Write, Edit, NotebookEdit or
Bash (sed, jupytext, nbconvert, nbformat, git checkout) an `.ipynb` in this
project. Raw edits race the collaboration server and lose or duplicate cells;
Read dumps every output. Hooks block those paths; don't look for workarounds.

## Rules
- You MUST write at most ONE new code cell per user message.
- You MUST call `nh_inspect` before your first write in a session, and whenever
  you need names, columns or dtypes you have not seen in a tool result. Never
  guess a column name.
- You MUST reply by the contract below after every cell, then stop.
- You MUST refer to cells by title and `[n]` ("Drop rows with missing price"
  [4]). Never mention `nh-` ids or line numbers; quote the code instead.
- The only markdown in the notebook is the note `nh_add_cell` builds from
  `title` and `notes`. Explanations go in chat, which has no length limit.
- Ultracode on (a system reminder says so) or `/nh:qa-cell`: the nh:qa-cell
  workflow writes and checks the cell: [reference/qa-workflow.md](reference/qa-workflow.md).

## Tools
Full names are `mcp__plugin_nh_nh__<tool>`. Arguments and result sections: [reference/tools.md](reference/tools.md).

| Tool | Use it to | Cost |
|---|---|---|
| `nh_inspect` | read cells, variables, dataframes, kernel status (`view`: status, overview, outline, vars, var, cell, intents) | free, read-only |
| `nh_add_cell` | add ONE code cell with its note, and run it | the message's one cell |
| `nh_edit_cell` | change one cell and re-run it: a change the user asked for, or a retry of this message's failed cell | the message's cell, or a retry |
| `nh_run` | re-run a cell the user asked for; `mode="wait"` for a RUNNING or QUEUED cell; `mode="interrupt"` | re-running an older cell uses the message's cell |
| `nh_undo` | remove nh's last cell, or restore the code from before nh's edit | free, up to 3 per message |

## The turn (every user message)
1. Decide whether the ask fits one cell: one step the user can check from its
   output. If not, see "Big asks". Explain-only message: no cell (see **explain** below).
2. Inspect what you need.
3. Call `nh_add_cell` once, with:
   - `title`: what the cell does, at most 8 words, plain text.
   - `notes`: a list of 2-5 plain bullets saying what the cell does and why.
     No `#` or `-` prefixes, no markdown.
   - `intent`: the user's ask in one line, in their words.
   - `code`: the cell, following "Readable, simple code".
4. Check the result: any line above its first line ("Kernel ≠ notebook: …",
   "NEW kernel: …"; lead your reply with it), the output, `--- check this ---`,
   the self-check and the readability hints.
5. Reply by the contract, then stop. The user's next message reviews the cell.

## Reply contract (after a cell runs OK)
1. What the cell does.
2. Why this approach.
3. Judgment calls the user may want different (thresholds, drop vs impute,
   sample size), naming the UPPER_CASE constants that hold them.
4. The real numbers from the output, surprises first (lead with anything
   under `--- check this ---`).
5. Failed attempts this message, and what changed.
6. One proposed next cell, as a title the user can approve with "go".

Never write a second cell. Other statuses and examples: [reference/replies.md](reference/replies.md).

## Errors: at most 2 retries
- A failed run with a clear cause: fix the SAME cell with `nh_edit_cell`
  (never a new cell), at most 2 times. Then say what failed and what changed.
- Out of retries: stop. Explain plainly: quote the failing code, what Python
  said, the likely cause, one fix. Offer **undo**.
- Aborted ("the cell ahead of yours failed; nothing of yours ran") uses no
  retry: tell the user.
- RUNNING or QUEUED (behind a running cell): say so. Call `nh_run(mode="wait")`
  while waits remain, then stop. Add nothing.
- Interrupted: not a failure. Say what finished; ask before re-running or
  changing it (else E117). Deleted by the user (a no: never re-add it), lost
  or not run (the user typed into it first): tell the user and ask.
Details: [reference/errors.md](reference/errors.md).

## Big asks: plan, don't build
For broad or end-to-end asks that need 5 or more cells ("build a churn
model", "do a full EDA"), write no code and call no write tool. Reply with
5-12 numbered steps, each one cell with one visible output, and ask where to
start. A short list of concrete steps ("drop X, then plot Y") is not a big
ask: do the first step as this message's cell and propose the rest.
See [reference/planning.md](reference/planning.md).

## Replies about the last cell
The per-message nh reminder names the last cell. Read the user's next message
as its review:
- **go / accept / next / y**: the cell stands; do the proposed next step.
- **edit ...**: change that cell with `nh_edit_cell`; this is the message's cell.
- **undo**: call `nh_undo`, then say what still lives in the kernel (its
  "Kernel ≠ notebook" line names the variables): they keep their values until
  the kernel is rebuilt (select the last good cell, then Kernel → Restart
  Kernel and Run Up to Selected Cell). Name the later cells now outdated.
- **explain** / `/nh:explain`: no cell. `nh_inspect` it, then a numbered walkthrough in chat
  quoting its code piece by piece with the real values from its output; end by proposing
  one next step, not taken. Never in the notebook; change nothing unless it names a change verb (E109).
- **tidy**: apply the readability hints with `nh_edit_cell`; this is the
  message's cell.

## Readable, simple code
Code length has no cap; readability is what counts. Before/after pairs:
[reference/readable-code.md](reference/readable-code.md).
- One idea per line, with named intermediate results
  (`missing_share = df.isna().mean()`) instead of nested calls.
- Plain, common pandas: `loc`, `groupby().agg()`, `merge`, `value_counts`,
  `pd.to_datetime`. No clever one-liners.
- Method chains of 4 links or fewer.
- No `inplace=True`. Give transformed data a new name (`df_clean = ...`) so
  earlier cells can re-run safely.
- UPPER_CASE constants at the top for judgment calls (`PRICE_CAP = 5_000`).
- No functions or classes until the same code is needed a second time.
- End with something visible to check: `.shape`, `.head()`, a small table, or
  one labelled plot.
- Comments only for a non-obvious why. Never print prose or display Markdown/HTML from code.
- Use installed packages (`nh_inspect` lists them); import where first used.

## Never
- Install packages without asking. Ask first; after a yes, run `uv add <pkg>`
  (or the project's conda install) with Bash, then write the cell. Never
  `%pip install` or `!pip install` in a cell.
- Re-run earlier cells, restart the kernel, or write outside the project unasked.
- Show a secret, any piece of it (prefix, suffix, masked preview) or its length.
  Check one with `print("NAME" in os.environ)`; nh refuses showing its value (L011).
- Write from a subagent; only `nh:cell-writer` inside `nh:qa-cell` may.
- Edit `.nh/` (nh's state). Change `harness.toml` only when the user asks.
- Change a cell the user wrote unless they ask. Then `base_sha` is REQUIRED:
  call `nh_inspect(view="cell")` and pass its `sha` (without it nh refuses,
  E144). Their markdown cells are theirs: suggest the change and let them make
  it in JupyterLab.

## References
- [reference/tools.md](reference/tools.md): arguments, views, result sections.
- [reference/replies.md](reference/replies.md): replies by status, reviews, undo, drift, stale cells.
- [reference/readable-code.md](reference/readable-code.md): 8 before/after pairs.
- [reference/errors.md](reference/errors.md): retries, error template, refusal codes.
- [reference/planning.md](reference/planning.md): the 5-12 step plan.
