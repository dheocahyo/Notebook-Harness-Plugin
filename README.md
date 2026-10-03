# Notebook Harness

Coding agents that drive Jupyter tend to write a whole notebook from one
prompt, and the data scientist then has to reverse-engineer cells they never
thought through. **Notebook Harness (nh)** is a Claude Code plugin that makes
the agent work in your live JupyterLab notebook **one reviewed cell per
message** instead, so the work stays yours.

It is a policy layer, not a new notebook: you keep JupyterLab, your kernel and
your files.

- **One cell per message.** The agent adds one code cell, runs it in your
  kernel and reports the real output. Your next message reviews it. Big asks
  get a numbered plan, not a notebook full of code.
- **Notes.** Above each cell: a title of at most 8 words and 2-5 bullets on
  what the cell does and why. Your ask is kept in the cell's metadata, so the
  notebook carries its own intent trail.
- **Readable code.** Plain pandas, named intermediate results, constants for
  judgment calls, something visible to check at the end.
- **Undo.** "undo" removes nh's last cell (or restores the code nh replaced),
  and nh tells you what still lives in the kernel and how to rebuild it.
- **Cell QA (optional).** Automatic under ultracode; otherwise type
  `/nh:qa-cell <ask>`. One agent writes the cell, and another checks its
  code, real output and kernel variables without running anything. The
  writer may fix the cell from QA's findings (twice by default) before
  Claude replies. It is still one cell per message, and one undo removes it.
  A cell nh asks you about first comes back to Claude, which asks you and,
  after your yes, writes it itself without a QA check.
- **Guardrails.** No raw `.ipynb` edits, no cells that print an env var's
  value, no writes from subagents except nh's own cell writer inside
  `/nh:qa-cell`, and a cell that installs packages, reaches a host your
  project hasn't approved, or writes outside the project, waits for your yes.
  nh's server enforces every rule and refuses what it can't verify.

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

The repository is a Claude Code plugin marketplace named `notebook-harness`
holding one plugin, `nh`. It is on GitHub at
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

In a folder with your data file, start `claude` and run **`/nh:init`**. It asks
three questions (goal, data, problem type), creates the project layout and
environment, starts JupyterLab, and adds the first cell: your data loaded, with
each column's type, nulls and distinct values. On the uv path this takes under
10 minutes.

## How a turn works

1. You ask: "drop the rows with missing price".
2. The agent looks at the live notebook and kernel (read-only) when it needs
   names or columns.
3. It adds one cell with its note and runs it; you see it appear in JupyterLab.
4. It replies: what the cell does, why, the judgment calls you might want
   different, the real numbers (surprises first), any failed attempts, and one
   proposed next cell.
5. You review: **go** (the proposed cell), **edit …**, **undo**, **explain**
   (a numbered walkthrough in chat, also as `/nh:explain`; nh blocks notebook
   changes in that message unless it also names one, such as "fix" or "add")
   or **tidy** (apply the readability hints).

If a cell fails, the agent fixes it in place, at most twice, then explains the
error in plain words.

## Settings

Project settings live in `harness.toml` at the project root; commit it. Every
key is in [docs/harness-toml.md](docs/harness-toml.md). When something doesn't
work, run `/nh:status` and see [docs/troubleshooting.md](docs/troubleshooting.md).

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

## Repository layout

| Path | What it is |
|---|---|
| `.claude-plugin/marketplace.json` | the `notebook-harness` marketplace |
| `plugins/nh/` | the plugin (the only part that ships): skills, the cell QA agents and workflow, hooks, `nhctl`, the MCP server in `server/`, eval suite in `evals/` |
| `docs/` | [design and module contracts](docs/design.md), [harness.toml reference](docs/harness-toml.md), [troubleshooting](docs/troubleshooting.md) |
| `dev/jupyter/` | the development JupyterLab (4.6 with collaboration 5) |
| `dev/sandbox/` | a small nh project for manual testing: `harness.toml`, `data/sales.csv`, `notebooks/01_eda.ipynb` and `.nh/` |
| `tests/` | unit, hook, nhctl, gateway, contract, integration and end-to-end tests |
| `spikes/` | early experiments; not shipped |

## Development

`dev/sandbox/` is a ready nh project (data, notebook, `harness.toml`, `.nh/`)
for trying the plugin from this checkout. Start its JupyterLab one of two ways,
from the repository root:

```sh
# a) the sandbox's own environment, as /nh:init sets one up
sh plugins/nh/bin/nhctl env sync --project dev/sandbox     # builds dev/sandbox/.venv (uv)
sh plugins/nh/bin/nhctl lab start --project dev/sandbox

# b) or the pinned development JupyterLab, serving the sandbox
uv run --project dev/jupyter jupyter lab --ServerApp.root_dir=dev/sandbox
```

Then, in a second terminal:

```sh
cd dev/sandbox && claude --plugin-dir ../../plugins/nh
```

With (b), nh finds the server by its root folder, and the kernel runs from
`dev/jupyter`'s environment. If `dev/sandbox/.venv` also exists (from a), nh
says so under `--- kernel ---` in each result; that is expected here. Don't run
both servers at once: two servers on one folder would overwrite each other's
notebooks. `nhctl lab stop` stops (a); Ctrl+C stops (b). nh's state in
`dev/sandbox/.nh/` and the sandbox's `.venv/` stay out of git.

`/reload-plugins` picks up skill and hook changes; `/mcp` → `plugin:nh:nh` →
Reconnect picks up server changes. `/plugin marketplace add ./` tests the
install path.

Tests run from the repository root. Integration and end-to-end tests start a
real JupyterLab, so install every dependency group first:

```sh
uv sync --project plugins/nh/server --all-groups
uv run --project plugins/nh/server pytest -q                    # unit, hooks, nhctl, gateway, contract
uv run --project plugins/nh/server pytest -q -m integration     # real JupyterLab
uv run --project plugins/nh/server pytest -q -m e2e             # full scenario
```

Check the plugin and the marketplace:

```sh
claude plugin validate --strict plugins/nh
claude plugin validate --strict .
```

The eval suite (`plugins/nh/evals/`) needs Claude Code 2.1.269+ and makes real
model calls. Cases seed their workspace with `evals/_scaffold/base.sh`, so pass
`--scaffold`:

```sh
cd plugins/nh
claude plugin eval . --scaffold --ablation none --tag ci --threshold 0.8
claude plugin eval . --scaffold --ablation none --tag write-tools --allow-tools Write Edit NotebookEdit --threshold 0.8
```

These match CI. `--ablation none` runs only the with-plugin arm; without it,
each case also runs without the plugin and reports the difference, and the
absolute scores differ between the two modes.

The MCP tools are mocked from `evals/mocks/nh/` (and a case's own `mocks/`).
The mock results follow the gateway's real output: `tests/unit/test_skill_files.py`
replays each mocked call on the eval fixture with the fake backend and fails
when a mock's shape (lead lines, first line, `nh:` line, sections) drifts.
`error-retry` uses `type: agent` mocks instead, so a run can fail, retry and
fix its cell: a model plays the gateway from the templates, rules and data
facts in `error-retry/mocks/nh/fixtures/nh-server.md`. The same test file
replays the states `AGENT_SCENARIOS` lists and fails when a template
(placeholders aside) drifts from the gateway's text in one of them, when the
"Which template" rule for a state stops naming its template, or when one of
the placeholder rules it checks (run numbers, retry and undo counts, cell
names, self-check forms and order, variable lines, error summary, cut output)
no longer holds. The file states pandas 2.2.3's error texts and dtypes;
`test_nh_server_pandas_facts` checks them only where pandas 2.2.3 is
installed. What the mock model plays is close to the gateway, not identical:
its self-check lines and headlines can differ (no grader reads them), it
never shows readability hints, and its tracebacks show only the cell's own
frame, as the test backend's do. Refusals start with `ERROR: `, which
`claude plugin eval` turns into a tool error, as the gateway returns them.
`_tools.json` is a saved copy of the server's tool list, generated by
`scripts/dump_tools.py`; regenerate it when a tool's parameters or description
change.

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
