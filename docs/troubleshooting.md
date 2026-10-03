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
| `.nh/logs/gateway.log` says "none of 3 new connections answered nh's kernel_info_request" | nh checks each new kernel connection, because JupyterLab's server sometimes leaves one dead from the start, and connects again on its own when one doesn't answer. Here none of three answered: they were dead, or the kernel was too occupied to answer although JupyterLab showed it idle (a background thread, for example). nh then uses the last connection, and a look or run on it may wait for its timeout | nothing, if it doesn't come back in a later session (nh notes it once per session). If it does: stop what keeps the kernel occupied, or interrupt or restart the kernel in JupyterLab; if that doesn't help, restart JupyterLab (`nhctl lab stop`, then `nhctl lab start`) |
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
| **E109** "no notebook change in an explain, plan or ask message" | by design: your message started with "explain" (or `/nh:explain`) and named no change verb (fix, change, add, update, rewrite, refactor, make), or asked to plan (`/nh:plan`) or to run several steps ("run next 3", which nh first asks about). A message like that typed while the agent works holds for the rest of that message | nothing to do: the agent answers in chat. To change the notebook, ask for the change in your next message ("explain and fix …" counts as a change) |
| The nh:qa-cell launch is refused: "approve_before_run = true" | the workflow runs in the background and can't ask you to approve its cell | nothing to do: the agent writes the cell itself. Set `[approval] approve_before_run = false` to use the workflow |
| The nh:qa-cell launch is refused: "already had its nh:qa-cell run" | one run per message | the agent replies from its report; ask again in your next message |
| A cell shows RUNNING | it runs longer than the tool call waits (100 s) | its output keeps arriving in JupyterLab; the agent can wait twice more; interrupt from JupyterLab if needed |
| A cell shows QUEUED | it waits behind another cell already running in the kernel (maybe yours) | wait, or stop the running cell in JupyterLab; the agent adds nothing meanwhile |
| **E117** stopped before it finished | the cell was interrupted (by you, or by the agent at your request) | say whether to re-run it, change it or leave it; nh doesn't re-run a stopped cell unasked |
| **E118** typed into before it ran | you edited nh's new cell before its run started, so nh ran nothing | say whether to run your version, restore nh's (undo) or leave it |
| **E122** "Not written: this needs the user's yes first." | by design: the cell does something nh asks you about before it writes it: installing packages, or removing them with `%pip uninstall`/`%conda remove` (L009), reaching a host your project hasn't approved (L012), or writing outside the project (L013). The agent asks you one question in chat and stops | answer **yes** (or **go**) on its own in your next message to have that exact cell written and run once; anything else (no, a question, "go on", "yes, but …") drops it, and nh asks again if the agent sends it later. Under ultracode or `/nh:qa-cell` the workflow's cell writer can't ask you: its report brings nh's question back, the agent asks you, and after your yes writes that exact cell itself, without a QA check. A yes you type while the workflow still runs, before its report, approves nothing: you haven't seen nh's question yet, so nh asks it when the agent sends the cell. With `NH_HEADLESS=1` (set it for headless runs such as `claude -p`; nh can't tell them apart otherwise) nobody can answer, so nh refuses: run it in an interactive session |
| **L009** the agent asks before a cell that installs a package | a cell with `!pip install`, `%pip install`, `%conda install`, `!uv add` and the like. A kernel-only install is gone after the next `nhctl env sync`; `uv add <pkg>` (or `environment.yml` for conda) keeps it in the project. The question names the packages, where they come from (an index, a channel, a local path) and what the cell does with each (installs, adds, removes) | say yes to run the cell as it is, or ask for `uv add <pkg>` instead. Set `[lint.rules] package_install = "error"` to refuse such cells outright, or `"off"` to let them run without asking. Some forms aren't caught (an install after `&&` or a path in a `%%bash` cell, `!pip uninstall`), and re-running an install cell doesn't ask: nh's skill tells the agent to ask you first anyway |
| **E125** "Not written: the code holds nh's [redacted:…] marker, not the real value." | nh hides secrets (tokens, passwords, keys from `.env` or the environment) from the agent as `[redacted:NAME]`, and the agent copied that marker into a cell | nothing to do: the agent reads the value from the environment or `.env` without printing it, or asks you to type that line in JupyterLab (a marker like `[redacted:password]` names no variable). nh never writes the real value back for it. If the hidden value is no secret: nh hides a setting (in `.env` or the environment) whose name looks secret (TOKEN, SECRET, PASSWORD, KEY…), and any other `.env` value of 16+ characters unless its name ends in a word for a place or label (`_PATH`, `_DIR`, `_URL`, `_HOST`, `_NAME`, `_SCHEMA`…). Rename it to end in such a word (`MODEL_VARIANT` → `MODEL_VARIANT_NAME`) or unset it |
| **L011** a cell was rejected: it "would show the value of env var …" (or "every env var's value", "every value in `.env`") | by design: the code would print a value from the environment or `.env` (`print(os.environ["KEY"])`, `os.environ.keys()`, `%env`, `!printenv`, `!cat .env`), so nh wrote nothing. Every env var counts, secret or not: nh can't tell which values are secrets | nothing to do: the agent checks it without the value (`print("KEY" in os.environ)`) or lists only the names (`sorted(os.environ)`). To see a value that is no secret, look it up yourself, or set `[lint.rules] secret_print = "hint"` in `harness.toml`; nh still hides the values it knows as secrets from the agent |
| **L014** hint: a shown name "says it holds a secret" | a cell printed a variable named like a secret (`api_key`, `db_password`) | check the output in JupyterLab; the agent shows `bool(api_key)` instead next time. `secret_name = "off"` turns the hint off; under `[lint] mode = "strict"` it rejects the cell like any hint |
| **E133** kernel busy | another cell (maybe yours) is running | wait for it, or interrupt it in JupyterLab |
| **E140** cell not found | the cell was moved, re-created or deleted in JupyterLab | the agent re-reads the outline; nothing to do |
| **E141** you changed the cell | nh won't overwrite your edits silently, and doesn't run a cell you typed into before it started (the reminder then says "not run") | the agent shows the change and asks first |
| **E142** already deleted | you deleted the cell in JupyterLab, so there is nothing to undo | nothing to do; the agent says what is still in the kernel |
| **E144** a cell you wrote | nh changes your cells only from the version the agent last looked at | nothing to do: the agent reads the cell and passes its version |
| **E145** a markdown cell | nh only writes code cells; markdown cells are yours | make the change in JupyterLab; the agent suggests the wording |
| A result starts with "Kernel ≠ notebook" | an undone cell's results are still in the kernel | select the last cell to keep, then Kernel → Restart Kernel and Run Up to Selected Cell. The line goes away once the kernel restarts or those variables are gone |
| A cell is marked STALE | it used data that changed since it ran | re-run it (or ask the agent to, next message) |
| Ctrl+Z doesn't remove a cell nh added | JupyterLab's undo only covers your own typing | say **undo**, or delete the cell |
| **L012** the agent asks before a cell that reaches the network | a cell that reads a URL from a host your project hasn't approved (`pd.read_csv("https://…")`, `s3://…`), or uses `requests`, `httpx`, `urllib`, `socket`, `!curl`, `!wget`, `!kaggle`, `!pip download`, `!git clone`, `!scp`, `!ssh` (also through `os.system`, `subprocess` or `%%bash`). The question names the hosts (a bucket as `s3://<bucket>`). A host is approved when it is in `.nh/state/approved_hosts.json`: `/nh:init` puts your data URL's host there. A subdomain isn't covered by its parent. It can also ask about a cell that only keeps a URL as a value in a call nh doesn't know (`cache.get(URL)`, `make_row(URL)`, `df["src"].map({URL: …})`): the question then names a host the cell never contacts. It misses a URL nh can't read in the code (from the env given to a reader, built at run time), a fetch with no URL in the cell (`sklearn.datasets.fetch_*`, `seaborn.load_dataset`, `!python fetch.py`) and download tools it doesn't know | say yes to run that one cell as it is; your yes doesn't approve the host, so the next such cell asks again. To approve a host for good, add it to `.nh/state/approved_hosts.json` (a JSON list of host names, such as `["data.example.org", "s3://trips-bucket"]`; it is local to your clone). For a question about a host the cell never contacts, say yes, or set `[lint.rules] network = "hint"` (or `"off"`); don't approve the host, which would let real requests to it through too. Or download the file into `data/raw/` and ask for the cell to read it there. Set `network = "error"` to refuse such cells outright |
| **L013** the agent asks before a cell that writes outside the project | a cell that writes, creates or removes a file or folder outside your project folder: a path starting with `~`, an absolute path elsewhere (`/data/…`, `/Users/…`), or a relative path that climbs above the project with `..` (counted from the notebook's folder: from `notebooks/`, `../data/` is still inside). Writers include `df.to_csv` and the other `.to_*` writers, `savefig`, `open(…, "w")`, `Path.write_text`, `shutil.copy`/`move`/`rmtree`, `os.remove`, `%%writefile`, and shell `>`, `tee`, `cp`, `mv`, `rm`, `mkdir`, `touch`, `curl -o`, also inside a function the cell or an earlier cell defines, where the call's path counts (`export(df, "~/x.csv")`; a function from an earlier cell whose path needs no argument asked in that cell, not again at a later call). The question names the paths. `/dev/null` (all of `/dev`) and the system temp folders (`/tmp`, `/var/tmp`, and on macOS `/private/tmp`, `/private/var/tmp`) never ask. Every `~` path asks, also one that leads back into your project (`~/proj/data/x.csv` with the project at `~/proj`: the question then calls it outside) and where a library would make a folder named `~` instead of using your home folder; so can a `.save(…)` of a library that writes no file, given an absolute path. It misses a path nh can't read in the code (from the env, a function's result, an attribute, a loop, a path given to a method), writers it doesn't know (a database file, `h5py`, a library's download cache, a script the cell runs), a `%cd` in an earlier cell and a folder change inside a function the cell calls; re-running a cell doesn't ask | say yes to run that one cell as it is; the next such cell asks again. Or ask for the files to go inside the project (for example `data/processed/` or `reports/`). For a question you don't need, set `[lint.rules] outside_write = "hint"` (or `"off"`); set `"error"` to refuse such cells outright |
| A cell was rejected with L0xx rules | it broke a hard rule (note shape, separators, notebook writes, markdown output, printing an env var's value) | nothing to do: the agent fixes it; after 3 rejections it explains and asks you. An install (L009) is no rejection: nh asks you first (**E122**), as for a cell that reaches a host your project hasn't approved (L012) or writes outside the project (L013) |
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
| `.nh/logs/jupyterlab.log` | the JupyterLab `nhctl lab start` launched, and what its kernels write straight to their output (subprocess, `os.system`, C libraries), raw. `nhctl lab status --log` shows its tail with secrets hidden |
| `.nh/log.jsonl` | one event per turn and cell (no code, no outputs), used by `nhctl metrics` |
| `.nh/state/` | turn records, stamps, last cell, env and JupyterLab records |

nh never writes the JupyterLab token into these files. Other secrets can reach
the logs raw when a tool prints them: uv or conda in `env-sync.log`, a kernel's
subprocess in `jupyterlab.log`. nh shows the agent their tails through `nhctl`,
with secrets hidden. Don't edit `.nh/` by hand; nh blocks the agent from doing
so too.

## Reinstall or remove

- Rebuild nh's runtime:
  `nhctl runtime sync --plugin-data ~/.claude/plugins/data/nh-notebook-harness`,
  then `/mcp` → `plugin:nh:nh` → Reconnect.
- Uninstall: `claude plugin uninstall nh@notebook-harness`. Add `--keep-data`
  to keep nh's runtime for a later reinstall. Projects keep their
  `harness.toml` and `.nh/`; delete them to remove nh from a project.
