# Notebook Harness (nh)

A Claude Code plugin that makes the agent work in your live JupyterLab
notebook **one reviewed cell per message**. It is a policy layer, not a new
notebook: you keep JupyterLab, your kernel and your files, and the agent
works in the same notebook you are looking at.

- **One cell per message.** The agent adds one code cell, runs it in your
  kernel and reports the real output. Your next message is the review of that
  cell. Big asks get a numbered plan instead of a notebook full of code.
- **Notes.** Above each cell sits a short note: a title of at most 8 words and
  2-5 bullets on what the cell does and why. The ask behind each cell is kept
  in the cell's metadata, so the notebook carries its own intent trail.
- **Readable code.** Plain pandas, named intermediate results, constants for
  judgment calls, something visible to check at the end.
- **Undo.** Say "undo" to remove nh's last cell (or restore the code nh
  replaced). nh tells you what still lives in the kernel and how to rebuild it.
- **Cell QA (optional).** Automatic under ultracode; otherwise type
  `/nh:qa-cell <ask>`. One agent writes the cell, and another checks its
  code, real output and kernel variables without running anything. The
  writer may fix the cell from QA's findings (twice by default) before
  Claude replies. It is still one cell per message, and one undo removes it.
- **Guardrails.** No raw edits to `.ipynb` files, no cells that print an env
  var's value, no writes from subagents except nh's own cell writer inside
  `/nh:qa-cell`, and a cell that installs packages, or reaches a host your
  project hasn't approved, waits for your yes. nh's server enforces every
  rule and refuses what it can't verify.

## Requirements

| Needs | Check |
|---|---|
| macOS or Linux | Windows: run Claude Code and JupyterLab under WSL |
| Claude Code 2.1.282 or newer | `claude --version`; upgrade with `claude update` |
| uv 0.10 or newer | `uv --version`; [install uv](https://docs.astral.sh/uv/getting-started/installation/). Projects can use conda instead, but nh's own runtime uses uv |
| A browser | for JupyterLab |

`/nh:init` installs JupyterLab 4.6+ and jupyter-collaboration 5 into each
project's own environment. A global JupyterLab is never used.

## Install

nh ships in the `notebook-harness` marketplace, on GitHub at
[dheocahyo/Notebook-Harness-Plugin](https://github.com/dheocahyo/Notebook-Harness-Plugin).

In Claude Code:

```
/plugin marketplace add dheocahyo/Notebook-Harness-Plugin
/plugin install nh@notebook-harness
```

Or from a shell:

```sh
claude plugin marketplace add dheocahyo/Notebook-Harness-Plugin
claude plugin install nh@notebook-harness --scope project
```

To work on nh itself, add your clone instead. Claude Code then loads the plugin
straight from the clone at each session start:

```sh
claude plugin marketplace add <path-to-clone>
```

Restart Claude Code. The first session builds nh's Python runtime (about a
minute) in `~/.claude/plugins/data/nh-notebook-harness/`; `claude --init-only`
does it ahead of time.

## Quick start

In a folder with your data file:

```sh
claude
```

then run **`/nh:init`**. It asks three questions (goal, data, problem type),
creates the project layout and environment, starts JupyterLab and adds the
first cell: your data loaded, with a table of each column's type, nulls and
distinct values. On the uv path this takes under 10 minutes.

## How a turn works

1. You ask: "drop the rows with missing price".
2. The agent looks at the live notebook and kernel (read-only) when it needs
   names or columns.
3. It adds one cell with its note, below the last one, and runs it. You see it
   appear in JupyterLab.
4. It replies: what the cell does, why, the judgment calls you might want
   different, the real numbers (surprises first), any failed attempts, and one
   proposed next cell.
5. You review:

| You say | nh does |
|---|---|
| **go** | the proposed next cell |
| **edit** … | changes that cell and re-runs it |
| **undo** | removes the cell; tells you what is still in the kernel |
| **explain** … | walks through the cell step by step in chat; nh blocks notebook changes in that message unless it also names one ("fix", "add", "make", …) |
| **tidy** | applies the readability hints to that cell |

If a cell fails, the agent fixes it in place, at most twice, then explains the
error in plain words.

## Commands

| Command | What it does |
|---|---|
| `/nh:init` | set up a project in this folder, or adopt an existing notebook |
| `/nh:status` | check every part of the setup, with fixes |
| `/nh:explain [cell]` | a numbered walkthrough of a cell (the last one by default) in chat, changing nothing; nh enforces it unless the text names a change ("fix", "add", "make", …) |
| `/nh:qa-cell <ask>` | one agent writes this message's cell, another QA-checks it (automatic under ultracode) |
| `nhctl lab start`, `status`, `stop` | the project's JupyterLab |
| `nhctl doctor` | the same checks as `/nh:status`, from a shell |
| `nhctl fresh-run` | run a copy of the notebook top to bottom in a fresh kernel |
| `nhctl metrics summarize` | cells per message, undos and rejections, from `.nh/log.jsonl` |

`nhctl` is on Claude Code's Bash PATH while nh is enabled.

## Settings

Project settings live in `harness.toml` at the project root; commit it. For
example, `[approval] approve_before_run = true` makes Claude Code ask before
every cell runs. Every key is documented in `docs/harness-toml.md` in the
Notebook Harness repository.

## Limitations

- macOS and Linux only; on Windows use WSL.
- JupyterLab 4.6+ with jupyter-collaboration 5 only. VS Code notebooks and the
  classic Notebook interface are not supported.
- Ctrl+Z in JupyterLab only undoes your own typing; it doesn't remove nh's
  cells. Say **undo**, or delete the cell.
- Undo changes the notebook, not the kernel: variables keep their values until
  you restart the kernel and re-run.
- Don't @-mention `.ipynb` files: that attaches every output to the
  conversation. Ask about the notebook in words.
- If you use Datalayer's `datalayer` plugin (or another Jupyter MCP server),
  disable it in nh projects; nh blocks its cell-changing tools.
- Only the main conversation writes cells, plus nh's own cell writer inside
  the `nh:qa-cell` workflow; other subagents can read.
- Cell QA doesn't run while `[approval] approve_before_run = true`: its
  agents can't ask you.
- Blocking shell commands that edit notebooks is best effort. For Claude
  Code's own edit tools, the deny rule `/nh:init` offers makes the block a hard
  permission rule.
- nh shows the agent secrets from `.env` and the environment as
  `[redacted:NAME]` (outputs, errors, cell code, nhctl's output); the notebook
  keeps the real values. It can miss a secret inside an image, an encoded one
  (base64, URL-encoded) and a short value (under 8 characters).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
