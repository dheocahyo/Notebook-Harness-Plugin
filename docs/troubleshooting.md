# Troubleshooting

Start with **`/nh:status`** in Claude Code: it runs `nhctl doctor` and asks the
nh server for its view, then prints OK or FAIL per check with the fix. From a
shell in the project, `nhctl doctor` shows the same checks.

Every nh refusal starts with a plain sentence and carries a code (`nh: E130`).
Find the code below.

## Setup and connection

| Symptom | Cause | Fix |
|---|---|---|
| `/nh:init` doesn't exist | the plugin isn't installed or enabled | `/plugin` → check `nh@notebook-harness` is enabled; restart Claude Code |
| nh's tools are missing, or `/mcp` shows `plugin:nh:nh` failed | the first start after installing builds nh's Python runtime (about a minute), or the server stopped | wait, then `/mcp` → `plugin:nh:nh` → Reconnect. `nhctl runtime status` shows progress. Claude Code never restarts a crashed MCP server by itself |
| **E137** Windows | nh v0.1 runs on macOS and Linux | run Claude Code and JupyterLab inside WSL |
| **E106** no prompt ids | Claude Code older than 2.1.196 | `claude update` (nh needs 2.1.282 or newer) |
| **E101** "hooks didn't stamp this call" | nh's hooks aren't running: Claude Code too old, no usable `python3` (3.9+), or a hook crashed | `/nh:status`; on macOS without developer tools run `xcode-select --install` or install Python; see `.nh/logs/hooks.log` |
| **E101** "Two identical calls arrived together" | the main conversation and a subagent made the same call at the same moment, so nh can't tell whose it is | nothing to do: the agent retries once |
| **E105** not an nh project | no `.nh/` folder here or above | run `/nh:init`, or open Claude Code in the project folder |
| `nhctl: command not found` in the Bash tool | the plugin's `bin/` isn't on PATH yet | restart Claude Code; in the meantime `/nh:init` uses `sh "${CLAUDE_PLUGIN_ROOT}/bin/nhctl"` |

## JupyterLab and the kernel

| Symptom | Cause | Fix |
|---|---|---|
| **E130** can't find this project's JupyterLab | it isn't running, or the running one is another project's or a global install | `nhctl lab start` in the project (it uses the project's own environment). Stop it with `nhctl lab stop` |
| nh won't use a JupyterLab on another host set in `harness.toml` | `[jupyter].url` there accepts only loopback addresses, so a committed file can't send your token elsewhere | set `NH_JUPYTER_URL` and `NH_JUPYTER_TOKEN` in the environment you start Claude Code from |
| `nh_inspect(view="status")` says a URL "is not a plain URL" or "isn't a plain http(s) address" | the URL (in `harness.toml`, `.nh/state/lab.json`'s server record or a Jupyter runtime file) has a backslash, a space, a control character or an unusual host, which Python and `requests` could read as different hosts | write it as `http://127.0.0.1:<port>/`, or restart JupyterLab with `nhctl lab start` |
| `nhctl lab start` says "already running (started outside nh; nh uses it)" | a JupyterLab you started yourself already serves this project | nothing to do: nh uses it instead of starting a second one. If its kernel isn't from the project env, `--- kernel ---` says so |
| **E131** no real-time collaboration | the server lacks jupyter-collaboration 5, often an old global JupyterLab | `nhctl lab start`; never upgrade the global install for nh |
| **E132** notebook not found | a wrong path, or the file was moved | pass the path relative to the project, or fix `[project].notebook` in `harness.toml` |
| **E134** no usable kernel | the notebook has no kernel, or it died | select or restart the kernel in JupyterLab |
| **E135** couldn't sync | the collaboration connection dropped | reload the notebook's browser tab, then ask again |
| **E136** unsupported notebook | not a Python kernel, or not nbformat 4 | nh can't work in this notebook |
| **E138** open in another Jupyter server | two servers would overwrite each other | close the notebook there or stop that server |
| `--- kernel ---` says "The kernel runs from …, not the project env" | the notebook uses another kernelspec, or JupyterLab was started from another environment | pick the project's kernel (Kernel → Change Kernel) or start JupyterLab with `nhctl lab start`; until then packages may differ from the project's |
| A result starts with "NEW kernel: earlier variables are gone" | the kernel restarted or was replaced since nh's last call | select the cell to rebuild up to, then Kernel → Restart Kernel and Run Up to Selected Cell (or ask the agent to re-run what you need) |
| `nhctl env sync` is slow | conda solves take 3-10 minutes; uv takes under a minute with a warm cache | wait (`nhctl env wait`); install uv for faster setups |
| `nhctl env sync` failed | a dependency couldn't be resolved or downloaded | read `.nh/logs/env-sync.log`, fix the env file, run `nhctl env sync` again |

## Cells and turns

| Symptom | Cause | Fix |
|---|---|---|
| **E110** "one new cell per message" | by design: the agent tried a second cell | answer with **go** (or pick a step) to get the next cell |
| **E112**, **E113** | by design: the message's cell already ran, or is another cell | review the result; ask for the change in your next message |
| **E102** "belongs to an earlier message" | by design: the call came from a message you've since replied to, or from a background task that finished while no message of yours was open (a message you type while the agent handles that task joins the task's turn, which has no cell) | nothing to do: the agent stops and waits for your next message; send again anything you typed while it handled the task |
| **E102** "nh missed this message; send it again." | nh's prompt hook didn't record your latest message (it failed or timed out), so nh can't tell what that message asked for and writes nothing for it | the agent asks you to send the message again; do so. If it keeps happening, see `.nh/logs/hooks.log` and run `/nh:status` |
| **E108** "the nh:qa-cell workflow is writing this message's cell" | by design: while the workflow writes and checks the cell, the main agent writes nothing | wait for its report, or stop the workflow to change course. A run that never reports holds only its own message, for at most an hour |
| The nh:qa-cell launch is refused: "approve_before_run = true" | the workflow runs in the background and can't ask you to approve its cell | nothing to do: the agent writes the cell itself. Set `[approval] approve_before_run = false` to use the workflow |
| The nh:qa-cell launch is refused: "already had its nh:qa-cell run" | one run per message | the agent replies from its report; ask again in your next message |
| A cell shows RUNNING | it runs longer than the tool call waits (100 s) | its output keeps arriving in JupyterLab; the agent can wait twice more; interrupt from JupyterLab if needed |
| A cell shows QUEUED | it waits behind another cell already running in the kernel (maybe yours) | wait, or stop the running cell in JupyterLab; the agent adds nothing meanwhile |
| **E117** stopped before it finished | the cell was interrupted (by you, or by the agent at your request) | say whether to re-run it, change it or leave it; nh doesn't re-run a stopped cell unasked |
| **E118** typed into before it ran | you edited nh's new cell before its run started, so nh ran nothing | say whether to run your version, restore nh's (undo) or leave it |
| **E133** kernel busy | another cell (maybe yours) is running | wait for it, or interrupt it in JupyterLab |
| **E140** cell not found | the cell was moved, re-created or deleted in JupyterLab | the agent re-reads the outline; nothing to do |
| **E141** you changed the cell | nh won't overwrite your edits silently, and doesn't run a cell you typed into before it started (the reminder then says "not run") | the agent shows the change and asks first |
| **E142** already deleted | you deleted the cell in JupyterLab, so there is nothing to undo | nothing to do; the agent says what is still in the kernel |
| **E144** a cell you wrote | nh changes your cells only from the version the agent last looked at | nothing to do: the agent reads the cell and passes its version |
| **E145** a markdown cell | nh only writes code cells; markdown cells are yours | make the change in JupyterLab; the agent suggests the wording |
| A result starts with "Kernel ≠ notebook" | an undone cell's results are still in the kernel | select the last cell to keep, then Kernel → Restart Kernel and Run Up to Selected Cell. The line goes away once the kernel restarts or those variables are gone |
| A cell is marked STALE | it used data that changed since it ran | re-run it (or ask the agent to, next message) |
| Ctrl+Z doesn't remove a cell nh added | JupyterLab's undo only covers your own typing | say **undo**, or delete the cell |
| A cell was rejected with L0xx rules | it broke a hard rule (note shape, separators, installs, notebook writes, markdown output) | nothing to do: the agent fixes it; after 3 rejections it explains and asks you. For an install (L009) it asks you first and installs with a command you approve |
| The agent printed every output of the notebook | the notebook was @-mentioned, which attaches the whole file | don't @-mention `.ipynb` files; ask about the notebook in words |
| Another Jupyter MCP tool was blocked | nh blocks other servers' cell-changing tools in nh projects | disable that server (for example Datalayer's `datalayer` plugin) in this project, or set `[guard] foreign_mcp` |
| nbstripout renumbers cell ids | nbstripout without `--keep-id` | reinstall its filter with `nbstripout --install --keep-id`; nh also matches cells by its own metadata |
| **E199** internal error | a bug in nh; nothing more was written | see `.nh/logs/gateway.log`; please report it |

## Logs and state

| File | Holds |
|---|---|
| `.nh/logs/gateway.log` | nh's MCP server |
| `.nh/logs/hooks.log` | hook errors (hooks never block Claude Code on their own failure) |
| `.nh/logs/env-sync.log` | `nhctl env sync` |
| `.nh/logs/jupyterlab.log` | the JupyterLab `nhctl lab start` launched |
| `.nh/log.jsonl` | one event per turn and cell (no code, no outputs), used by `nhctl metrics` |
| `.nh/state/` | turn records, stamps, last cell, env and JupyterLab records |

Tokens never appear in these files. Don't edit `.nh/` by hand; nh blocks the
agent from doing so too.

## Reinstall or remove

- Rebuild nh's runtime:
  `nhctl runtime sync --plugin-data ~/.claude/plugins/data/nh-notebook-harness`,
  then `/mcp` → `plugin:nh:nh` → Reconnect.
- Uninstall: `claude plugin uninstall nh@notebook-harness`. Add `--keep-data`
  to keep nh's runtime for a later reinstall. Projects keep their
  `harness.toml` and `.nh/`; delete them to remove nh from a project.
