# nh tools: arguments and results

Full names: `mcp__plugin_nh_nh__nh_inspect`, `…__nh_add_cell`, `…__nh_edit_cell`,
`…__nh_run`, `…__nh_undo`. Every tool takes an optional `notebook`: a path
relative to the project. It defaults to the active notebook, which starts as
`harness.toml [project].notebook`. Limits come back as refusals with a
`Next:` line, never as silent truncation.

## nh_inspect (free, read-only)

| Argument | Default | Meaning |
|---|---|---|
| `view` | `"overview"` | one of the views below |
| `name` | | variable name, for `view="var"` |
| `cell_id` | | cell id, for `view="cell"`; with `outline`, centres the window on it |
| `rows` | `[inspect].head_rows` (5) | head rows for `view="var"` (at most 20) |

| View | Shows | Use it when |
|---|---|---|
| `status` | project, JupyterLab, collaboration, kernel (Python, prefix), hooks, project env | a tool failed, or at the start of `/nh:init` |
| `overview` | outline plus variables and installed packages | first call in a session |
| `outline` | one row per cell: index, id, kind, `[n]`, status (`ok`, `ERR`, `RUN`, `STALE`), author (`agent`, `human`, `agent*` = the user edited nh's code), title | finding a cell or its id |
| `vars` | every user variable: dataframe shape, columns, nulls; array shapes; scalars | before writing code that uses them |
| `var` | one variable in full, with a head of `rows` rows | checking values or dtypes |
| `cell` | one cell's source, intent, note bullets, outputs, and its `sha` | before editing a cell you didn't just write |
| `intents` | why each nh cell exists: title, intent, bullets, author | "why is this cell here?" |

Output is capped at `[inspect].max_chars` (4,000). Narrow the view rather than
repeating it.

## nh_add_cell (the message's one cell)

| Argument | Rule |
|---|---|
| `title` | what the cell does; at most 8 words; one line; plain text (no `#`, backticks, HTML) |
| `notes` | list of 2-5 bullets, each at most 40 words (aim for 25); plain text, no `-`/`*`/numbers in front, no headings, fences, images, HTML or nested lists |
| `intent` | the user's ask in one line, in their words (at most 200 characters) |
| `code` | one step; no `# %%` or `# In[ ]` separators, no package installs, no writes to `.ipynb`, no Markdown/HTML output |
| `after_cell_id` | only when the user asks for a position; default is the bottom |

```json
{"title": "Drop rows with missing price",
 "notes": ["Keeps only orders that have a price, in a new frame df_clean",
           "Mean prices by region would be skewed if missing prices counted as zero"],
 "intent": "drop the rows where price is missing",
 "code": "df_clean = df.dropna(subset=[\"price\"])\ndf_clean.shape"}
```

A hard-rule rejection (`E120`, with `L0xx` lines) writes nothing and does not
use your cell: fix every listed problem and call again. Two rules differ:
- `L002` (separators): keep only the first step in this cell, with no
  separators, and propose the rest in your reply.
- `L009` (a package install), when `harness.toml` makes it an error: don't call
  again yet. Ask the user whether to install the package; after a yes, run
  `uv add <pkg>` with Bash (in a conda project: add it to environment.yml, then
  `nhctl env sync`), then write the cell without the install.

By default a package install is no rejection: nh asks the user first (`E122`,
with the question in `Next:`). Ask it, stop, and after the user's yes send the
exact same call again ([asks.md](asks.md)).

After 3 rejections in one message you get `E121`: stop and tell the user what
you are trying to write.

## nh_edit_cell

| Argument | Rule |
|---|---|
| `cell_id` | the code cell to change |
| `code` | the new code for the whole cell |
| `base_sha` | `sha` from `nh_inspect(view="cell")`. REQUIRED for a cell the user wrote: without it nh refuses (`E144`). Pass it too for an nh cell you did not write this message |
| `title`, `notes`, `intent` | optional; update the note above nh's own cells (same rules as above). Human cells never get a note |

Markdown cells are the user's: editing one is refused (`E145`). Suggest the
wording and let the user change it in JupyterLab.

Uses: the change the user asked for this message (the message's cell), or a
retry of the cell you wrote this message after it failed (at most 2). After a
cell ran OK, editing it again this message is refused (`E112`); editing a
different cell is refused (`E113`). Inside the nh:qa-cell workflow, nh's cell
writer may revise its OK cell up to `[turn] max_revisions` times; nh ignores
its `base_sha`, so it never changes the user's cells or edits (`E144`, `E141`).

## nh_run

| `mode` | Does | Cost |
|---|---|---|
| `"run"` (default) | re-runs the cell unchanged | an older cell uses the message's cell; only when asked |
| `"wait"` | waits up to about 100 s more for a cell reported RUNNING or QUEUED | free, 2 per message |
| `"interrupt"` | stops nh's running cell (only after it started) | free |

## nh_undo

| Argument | Rule |
|---|---|
| `cell_id` | default: nh's most recent change in this session's last 3 messages |
| `force` | `true` only after the user confirms overwriting their own edits (`E141`) |

Undo removes a cell nh added (with its note) or restores the code from before
nh's edit this message; retries in the same message fold into one step. It
never rolls back the kernel: a `Kernel ≠ notebook: …` line above the result
names the variables that still hold undone results. Up to 3 undos per message,
most recent first.

## Result sections

The first line is for the user, for example:

```text
Added "Drop rows with missing price" [2] at the bottom, below "Load raw data and check schema" [1]; ran ok in 0.4s; df_clean: new DataFrame 37×6 (from df 43×6); no nulls.
```

Lines ABOVE it warn about the kernel. Lead your reply with them:
- `Kernel ≠ notebook: …`: named variables still hold results of an undone
  cell; the line gives the menu path to rebuild the kernel.
- `NEW kernel: earlier variables are gone …`: the kernel restarted or was
  replaced since nh's last call. Cells that use earlier names fail until the
  cells above re-run.

Then:

| Section | Holds |
|---|---|
| `nh:` line | machine fields: cell id, `exec`, turn, retries, waits, undos; for nh's cell writer only, also `revisions` (QA revisions used/allowed). Never repeat the id to the user |
| `--- check this ---` | surprises: rows went to 0, over half the rows removed, rows grew after a merge, an all-null new column, unchanged shape despite a drop. Lead your reply with these |
| `--- output ---` | the real output (head and tail, at most 2,000 characters; full copy under `.nh/outputs/`) and up to 2 images. Both show secrets as `[redacted:NAME]`; never copy a marker into code (E125) |
| `--- error ---` | the error and the failing code line, when the cell failed |
| `--- self-check ---` | before → after: frame shapes and nulls, new and removed names |
| `--- readability hints (advisory) ---` | up to 5 hints quoting the code; offer "tidy" |
| `--- kernel ---` | "The kernel runs from …, not the project env": packages may differ from the project's; tell the user |
| `--- stale ---` | cells now outdated, by title and `[n]` |
| `--- notices ---` | one-off notes, such as a note left unchanged by an edit (check it still fits the code) |
| `--- config ---` | unknown or misplaced `harness.toml` keys |
| `--- next ---` | what your reply must contain for this status |

Statuses: `ok`, `error` (retries left or not), `running`, `queued` (not
started: waiting behind a cell already running; treat it like `running`),
`aborted` (a cell ahead of yours failed), `timeout`, `interrupted` (stopped
before it finished; ask before re-running or changing it), `deleted` (the user
deleted it in JupyterLab while it ran), `lost` (the kernel restarted or went
away). The per-message reminder can also say `conflict` (not run: the user
typed into the cell before it started) or `undone`. Replies per status:
[replies.md](replies.md). Error codes: [errors.md](errors.md).
