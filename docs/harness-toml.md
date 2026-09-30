# harness.toml reference

`harness.toml` sits at the project root and holds Notebook Harness (nh)
settings for that project. Commit it, so everyone working on the project gets
the same rules. `/nh:init` (`nhctl scaffold`) writes it with the chosen
`[project]` values; every other section is there with each of its keys
commented out at its default (`# max_retries = 2`). Uncomment a key to change
it. A commented key keeps following nh's built-in default, including when a
later nh version changes that default.

- **Every key is optional.** A missing key uses the built-in default below.
- **Precedence:** built-in defaults < `harness.toml` < `NH_*` environment
  variables.
- **Reloads:** nh re-reads the file whenever it changes, before the next tool
  call. Only `[approval]` needs a restart of the MCP server: in Claude Code, run
  `/mcp`, pick `plugin:nh:nh` and choose Reconnect.
- **Mistakes:** an unknown key, a key in the wrong section or a value of the
  wrong type is ignored, and every nh result shows it under `--- config ---`
  until it is fixed.
- **Reserved:** the sections `[preset]`, `[guardrails]`, `[secrets]`,
  `[libraries]` and `[comprehension]` are accepted and ignored in v0.1.
- The agent changes this file only when you ask it to.

```toml
version = 1

[project]
goal = "Which regions drive the drop in average price?"
problem_type = "eda"

[lint]
mode = "strict"
```

## Top level

| Key | Default | Meaning |
|---|---|---|
| `version` | `1` | Format version of this file. |

## `[project]`

What the project is. `/nh:init` fills these in.

| Key | Default | Meaning |
|---|---|---|
| `name` | `""` | Short project name (a slug). |
| `goal` | `""` | The question the analysis should answer. Shown to the agent at session start. |
| `problem_type` | `"eda"` | One of `eda`, `classification`, `regression`, `clustering`, `forecasting`, `other`. |
| `data_source` | `""` | Where the data lives: a project-relative path, an absolute path, or a URL without credentials (a credentialed URL is kept in `.env` as `DATA_URL`). |
| `notebook` | `"notebooks/01_eda.ipynb"` | The notebook nh works in by default, relative to the project root. The agent can switch notebooks within a session. |
| `env_manager` | `""` | `uv` or `conda`; written by `/nh:init`. |

## `[jupyter]`

How nh finds your JupyterLab. Leave it empty to let nh find the project's own
server (the one `nhctl lab start` launched, then any running server whose root
is this project).

| Key | Default | Meaning |
|---|---|---|
| `url` | `""` | Base URL of a JupyterLab on this machine to use instead, e.g. `http://127.0.0.1:8890/`. Only loopback addresses (`127.0.0.1`, `localhost`, `[::1]`) are accepted here, because this file is committed and a cloned project must not send your token elsewhere. For a server on another host, set `NH_JUPYTER_URL` and `NH_JUPYTER_TOKEN` in your environment (below). The server must be JupyterLab 4.6+ with jupyter-collaboration 5+. Tokens never go in this file. |
| `kernel_name` | `""` | Kernelspec for a notebook that has no running kernel session. When set, nh tries it first, then the notebook's own kernelspec, then `python3`. Empty: the notebook's own kernelspec, then `python3`. |

Older files may still have `token_env`; it is no longer read, and nh lists it
under `--- config ---` as an unknown key until you delete it.

## `[approval]`

| Key | Default | Meaning |
|---|---|---|
| `approve_before_run` | `false` | `true`: Claude Code asks you before every `nh_add_cell` and `nh_edit_cell`, even in auto-accept modes. `false`: cells run straight away and you review the result (accept, edit, undo). Applies after `/mcp` → Reconnect. While `true`, the nh:qa-cell workflow doesn't start: its agents can't ask you. |

## `[turn]`

Per-message budgets. A "message" is one user prompt in Claude Code. A
background task's report (such as the nh:qa-cell workflow's) is not a
message: it shares the budget of the message it belongs to.

| Key | Default | Meaning |
|---|---|---|
| `max_code_cells` | `1` | New or changed cells per message. Keep 1: it is the core of nh. |
| `max_retries` | `2` | In-place fixes (`nh_edit_cell`) of this message's cell after it fails. |
| `max_revisions` | `2` | Changes the nh:qa-cell workflow's cell writer may make to this message's cell after it ran OK, from QA findings. A failed revision uses `max_retries`. Only the writer gets revisions. |
| `max_waits` | `2` | `nh_run(mode="wait")` calls per message for a cell still running. |
| `max_undos` | `3` | `nh_undo` calls per message. |
| `max_lint_rejects` | `3` | Rejected write attempts per message before nh asks the agent to stop and explain to you what it is trying to write. |
| `stamp_ttl_s` | `1800` | Seconds a hook's turn stamp stays valid. Older stamps are discarded. |

## `[exec]`

| Key | Default | Meaning |
|---|---|---|
| `soft_timeout_s` | `100` | After this many seconds the tool call returns "running" (Claude Code moves longer calls to the background at 120 s). The cell keeps running and its output keeps appearing in JupyterLab. |
| `hard_timeout_s` | `1800` | A cell nh started is interrupted after this many seconds. |
| `probe_budget_s` | `0.8` | Time budget for each read-only kernel probe (variables, shapes, nulls). |

## `[output]`

What a tool result may carry back to the agent. The notebook always keeps the
full output.

| Key | Default | Meaning |
|---|---|---|
| `max_chars` | `2000` | Characters of cell output per result (head and tail; tracebacks keep the tail). The full text is saved under `.nh/outputs/`, with secrets shown as `[redacted:NAME]` as in the result; the notebook keeps the raw output. |
| `max_images` | `2` | Images per result. |
| `image_max_px` | `768` | Longest side of an image sent to the agent, in pixels. |

## `[inspect]`

Limits for `nh_inspect`.

| Key | Default | Meaning |
|---|---|---|
| `max_chars` | `4000` | Characters per `nh_inspect` result. |
| `head_rows` | `5` | Rows in a dataframe head when `nh_inspect(view="var")` gets no `rows` argument. |
| `max_rows` | `20` | Most rows the agent may ask for. |
| `max_vars` | `40` | Most variables listed. |
| `outline_limit` | `60` | Most cells in an outline; longer notebooks show a window. |

## `[markdown]`

The note nh writes above each of its code cells: a `###` title and 2-5 bullets.

| Key | Default | Meaning |
|---|---|---|
| `title_max_words` | `8` | Longest title, in words (hard rule L003). Also the prose limit for L010. |
| `notes_min` | `2` | Fewest bullets (hard rule L004). |
| `notes_max` | `5` | Most bullets (hard rule L004). |
| `bullet_max_words` | `40` | Longest bullet, in words (hard rule L004). |
| `bullet_hint_words` | `25` | A bullet longer than this gets a hint (L123). |
| `heading_level` | `3` | Heading level of the title (`3` = `###`). |

## `[lint]`

Readability checks on each cell before it is written. Hard rules (below)
reject the cell; hints arrive with the result, after the run.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `"advise"` | `advise`: hints are advisory. `strict`: every hint is enforced as a hard rule. |
| `comment_ratio` | `8` | At most one comment per this many code lines (L105). `0` means no comments. |
| `max_line_length` | `99` | Longest line, in characters (L101). |
| `max_cell_lines` | `40` | Longest cell, in lines (L102). |
| `max_nesting` | `3` | Deepest nesting of blocks (L103). |
| `max_chain` | `4` | Longest method chain, in logical links across lines (L104). |

## `[lint.rules]`

Each rule is `"off"`, `"hint"` or `"error"`. `"error"` rejects the cell before
it is written; `"hint"` reports it after the run; `"off"` skips it. With
`secret_print` at `"hint"` or `"off"`, a cell can print an env var's value into
the notebook; nh still hides the values it knows as secrets from Claude
(`[redacted:NAME]`).

| Key | Rule | Default | Fires on |
|---|---|---|---|
| `package_install` | L009 | `"error"` | a package install in a cell: `!pip install`, `%pip install`, `%conda install`, `!uv add` and the like (`%pip list` is fine). The agent asks you and installs with a command you approve instead. |
| `notebook_write` | L008 | `"error"` | a cell that writes an `.ipynb` file |
| `markdown_output` | L010 | `"error"` | `%%markdown`, `%%html`, or `Markdown()`/`HTML()`/`Latex()` showing prose |
| `secret_print` | L011 | `"error"` | code that would show an env var's value, secret or not (nh can't tell which values are secrets): `print(os.environ["API_KEY"])`, `os.getenv("API_KEY")` as the last line, a variable holding one, `os.environ.keys()` shown (its repr holds every value), `%env`, `!env`, `!printenv`, `!echo $API_KEY`, `!cat .env`, a `.env` file read in Python and shown. Checking is fine: `print("API_KEY" in os.environ)`, `print(bool(os.getenv("API_KEY")))`, `sorted(os.environ)`, `len(key)`, passing it on (`create_engine(url)`), or an env var the cell set to a literal (`os.environ["MODE"] = "dev"`) |
| `secret_name` | L014 | `"hint"` | showing a name that says it holds a secret, such as `print(api_key)` or `print(db_password)` (not `tokens`, `tokenizer`, `max_tokens`, `token_counts`, `eos_token`, `has_api_key`, `author`) |
| `long_line` | L101 | `"hint"` | a line over `max_line_length` |
| `long_cell` | L102 | `"hint"` | a cell over `max_cell_lines` |
| `deep_nesting` | L103 | `"hint"` | nesting deeper than `max_nesting` |
| `long_chain` | L104 | `"hint"` | a chain longer than `max_chain` |
| `comment_budget` | L105 | `"hint"` | more comments than `comment_ratio` allows |
| `commented_out_code` | L106 | `"hint"` | code left in comments |
| `multi_statement` | L107 | `"hint"` | several statements on one line |
| `star_import` | L108 | `"hint"` | `from x import *` |
| `bare_except` | L109 | `"hint"` | a bare `except:` |
| `non_idempotent` | L111 | `"hint"` | a cell that changes its input in place (`df["p"] = df["p"] / 100`, `inplace=True`), so re-running it gives a different result |
| `apply_lambda` | L112 | `"hint"` | `apply(lambda …, axis=1)` |
| `cryptic_name` | L113 | `"hint"` | names like `df2`, `tmp`, `temp`, `data1`, single letters outside loops |
| `early_def` | L114 | `"hint"` | a `def` or `class` before the code is reused |
| `no_visible_output` | L116 | `"hint"` | a cell that shows nothing to check |
| `many_outputs` | L117 | `"hint"` | 3 or more display points (looks like several steps) |
| `hidden_warnings` | L118 | `"hint"` | `filterwarnings("ignore")`, `except: pass`, `%%capture` |
| `prose_print` | L119 | `"hint"` | `print` of more than 15 words |
| `kernel_only_name` | L120 | `"hint"` | a name no cell above defines, so the notebook won't run top to bottom |
| `intent_too_long` | L122 | `"hint"` | an intent over 200 characters |
| `long_bullet` | L123 | `"hint"` | a bullet over `bullet_hint_words` |

These hard rules have no switch: L001 (empty code), L002 (cell separators such
as `# %%`), L003 (title), L004 (bullets), L005 (missing intent) and L007
(syntax error for the kernel's Python version).

## `[stale]`

| Key | Default | Meaning |
|---|---|---|
| `mark` | `"none"` | How cells made outdated by an undo are shown. `none`: only in nh's outline and results. `clear-count`: also blank their `[n]` in JupyterLab. |

## `[guard]`

| Key | Default | Meaning |
|---|---|---|
| `foreign_mcp` | `"deny"` | Other Jupyter MCP servers' tools that change cells or run code. `deny`: blocked in nh projects (read-only tools stay allowed). `warn`: allowed with a warning. `off`: no check. |

## Environment variables

| Variable | Effect |
|---|---|
| `NH_JUPYTER_URL` | Overrides `[jupyter].url`. The only way to use a JupyterLab on another host. |
| `NH_JUPYTER_TOKEN` | Token for the server at `NH_JUPYTER_URL` or `[jupyter].url`. |
| `NH_HEADLESS=1` | Forces `approve_before_run = false`, for runs with nobody to answer prompts. |
