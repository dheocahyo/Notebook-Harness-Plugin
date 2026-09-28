# Spike results

Automated checks from plan §8 (M1). Stack: JupyterLab 4.6.4, jupyter-collaboration 5.0.4
(jupyter-server-ydoc 3.0.4, jupyter_server 2.21.1), jupyter-ydoc 4.1.1, pycrdt 0.14.6,
jupyter-nbmodel-client 1.5.1, jupyter-kernel-client 1.0.2, ipykernel 7.3.0, Python 3.13.8, macOS.
Date: 2026-09-25.

The regression tests for these rows live in `tests/integration/test_rtc_document.py`,
`tests/integration/test_kernel_exec.py` (run with `-m integration`), `tests/unit/test_probes.py` and
`tests/unit/test_discovery.py`. The S1 browser checks (m1–m7), S4, S5 and S6 #6 and #7 are recorded
under "v0.1.1 acceptance" at the end. S2 UI observations and S3 are still manual and are not recorded here.

## S1: RTC persistence

| Check | Result | Decision |
|---|---|---|
| a1: a second client sees both new cells, their explicit ids and `metadata.nh` | PASS, within one update: the peer's cells observer fires once with both cells | Insert the note and code cell with `create_ycell` in one `ydoc.transaction(origin=_changes_origin)` (no fallback needed) |
| a2: after the save delay, `nbformat.read` of the file shows the ids and `metadata.nh`; `nbformat.validate` passes; no `execution_state` on disk | PASS | Keep writing through the room only, never the Contents API |
| nbformat 4.4 notebook: ids survive saving | PASS after setting `_ymeta["nbformat_minor"] = 5` in the first write's transaction; pre-existing cells get ids too | Upgrade 4.4 → 4.5 on the first nh write, with a one-time notice |
| a3: `execution_count` is on disk after an nh run | PASS | — |
| a4: insert, disconnect at once, wait for room cleanup: cells are on disk | PASS (cleanup delay 3 s) | No extra wait before closing a room |
| a5: 20 insert/delete rounds with a reconnect each: no duplicate ids in the room or on disk | PASS | Resolve cells by id at write time; `generation` bumps on each reconnect |
| a6: `metadata.nh`, tags and source patches on an existing cell persist | PASS | Metadata keys are replaced by delete-then-insert inside the transaction |
| a7: very large outputs | Not measured (no 12 MB update sent) | Outputs are capped before they reach the room: stream tail 1 MB, bundle 5 MB, 8 MB per cell |
| a8: JupyterLab server restart while connected | Not tested automatically | A dropped room is detected by `synced`/task state; the next call reconnects with a fresh PUT; `ServerGone` from REST triggers rediscovery |
| Extension version vs collaboration version | `/lab/api/extensions?per_page=100` reports `jupyter-collaboration-extension` 5.0.4 = jupyter-collaboration 5.0.4 | Advisory only (status view); the hard gate stays `.nh/state/env.json` |
| Detecting collaboration without side effects | `GET /api/collaboration/session/<path>` answers 405 with collaboration installed, 404 without | Discovery uses it to reject collaboration-less servers (E130) |
| Room 404 for a missing file vs no collaboration | `PUT /api/collaboration/session/<p>` is 404 in both cases | Disambiguate with `GET /api/contents/<p>?content=0`: E132 missing file, else E131 |
| Awareness / presence | The room server relays awareness but never replays existing states to a newcomer; clients renew every 15 s | `NhNbModelClient` applies peers' awareness and re-announces itself when a new peer appears: a tab opened after nh connected sees "nh (agent)" in about 1 s |
| pycrdt `Text` offsets | UTF-8 byte offsets, not code points | difflib opcodes (code points) are converted to byte offsets; tested with accents and emoji |
| pycrdt value conversion | NaN is accepted; ints beyond int64 and lone surrogates panic (a `BaseException`); ints ≥ 2**53 are stored as Yjs BigInt, which JupyterLab can't JSON-serialize | `docsafe.sanitize_for_doc`: NaN/Inf → null, ints outside ±(2**53−1) → str, lone surrogates → U+FFFD |
| Reassigning a shared type (`ycell["source"] += "x"`) | Replaces the Text with an empty one | nh only edits Text in place (`insert`/`del`); tests do the same |
| A notebook with zero cells | The room adds an empty code cell when it loads the file | Nothing to do; noted for tests and `nhctl scaffold` |
| Room sync timeout | `NbModelClient.start()` ignores its timeout and only warns | nh runs `nb.run()` as a task and waits for `wait_until_synced()` or the task's end, 8 s cap (E135) |

## S2: kernel and output

| Check | Result | Decision |
|---|---|---|
| b1: attach to the session created with `POST /api/sessions` | PASS: same kernel, one session | Sessions lookup by path on every attach |
| b2: `JupyterKernelClient(kernel_id=…)` then `stop(shutdown_kernel=False)` | PASS: the user's kernel keeps running | Never `start()` a client whose `has_kernel` is false (E134); assert `channels_running` |
| b3: live output while the cell runs | PASS: a peer sees the first stream output while the cell is still running | Loop-side flusher every 200 ms, only changed outputs; p95 lag not measured |
| b4: execution counts | nh cells get `execution_count` from the reply (`execute_input` as fallback) | — |
| b5: silent probes leave no trace | PASS: no execution count used, `globals()` unchanged, silent `display()` output **is** published on iopub | Probes are `exec`'d inside a lambda with throwaway globals; no `user_expressions` fallback needed |
| `get_ipython().execution_count` | It is the count the **next** cell will get | The attach probe reports it as such |
| b6: probe while a cell runs | Refused with `KernelBusy` (nh's run, or REST busy twice 0.5 s apart) | Probes use their own client and never queue behind a run |
| b7: kernel restart mid-run | A graceful restart sends `shutdown_request`: iopub shows `shutdown_reply`, then our cell gets a `KeyboardInterrupt` error and `idle`, and **no shell reply**; REST never shows `restarting` (idle again within ~0.5 s) | Treat an iopub `shutdown_reply` (or server `restarting`/`dead` status) as a restart: the run is `lost` in about 1 s. REST polling every 2 s (404, dead, (re)starting, idle twice) is the backstop |
| b8: interrupt a sleeping cell | PASS: `KeyboardInterrupt` error, reply `error` | Status `interrupted` when nh asked for it |
| `stop_on_error` across clients | A user's failing cell sent with `stop_on_error=True` aborts nh's queued request (reply `aborted`) even though nh sends `stop_on_error=False` | Status `aborted`, uses no retry; nh cells always use `stop_on_error=False` |
| Hard timeout while queued behind a user's cell | PASS: nh's run times out as "never started" and the user's cell completes normally | Interrupt only after our own `busy`/`execute_input` |
| UUID-path session race | PASS: nh waits (up to 5 s) for JupyterLab's PATCH from `<dir>/<uuid>` and attaches to that kernel; no second kernel | As planned; several sessions for one path: the most connections wins |
| Unknown kernelspec in notebook metadata | `POST /api/sessions` answers 501 | Retry with `[jupyter].kernel_name`, then `python3` |
| Kernel websocket drops between runs | PASS: the next run connects a fresh client | `KernelHandle.client()` reconnects when `channels_running` is false |
| Room drops mid-run | PASS: outputs are rewritten into the new room document; the final count and state land | Flusher rewrites everything once per new `generation` |
| gateway exits mid-run | PASS: the cell is marked idle with a notice | `aclose()` aborts runs, which end as `lost` |
| Large output | 1.5 MB of stdout: the notebook keeps the last 1 MB with a notice; the result keeps it all | As planned |
| Probe sources on Python 3.11 (pandas 2.0) and 3.13 (pandas 3.0), with and without pandas installed | PASS | Probes touch pandas/numpy only through `sys.modules` |

## S6: cell QA workflow (Claude Code 2.1.282)

Headless runs in `dev/sandbox` with `claude -p … --plugin-dir ../../plugins/nh --output-format stream-json`
and a spike-only hook logger (removed afterwards). Date: 2026-09-26.

| Check | Result | Decision |
|---|---|---|
| 1: discovery and launch | `workflows/qa-cell.js` is discovered without a `plugin.json` key. The model launches it with `Workflow {name: "nh:qa-cell", args}`; the recorded `workflowName` is `qa-cell`. `/nh:qa-cell <ask>` also goes through a Workflow **tool call** (`args` is the raw string), so PreToolUse and PostToolUse fire for both paths | Accept `qa-cell` and `nh:qa-cell` as the name; hooks see every launch |
| 2: PostToolUse payload | `tool_input` is `{name, args, script}`: Claude Code injects the resolved script text, byte-identical to the plugin file. `tool_response` is `{status: "async_launched", taskId, taskType: "local_workflow", workflowName, runId, summary, transcriptDir, scriptPath}`. PreToolUse sees only `{name, args}` | "Own script" = sha256 of `tool_input.script` equals the plugin's `workflows/qa-cell.js`; no transcript parsing needed |
| 3: agent ↔ run binding | SubagentStart fires before the meta file exists, but by the writer's first PreToolUse `<transcriptDir>/agent-<agent_id>.meta.json` exists with `agentType: "nh:cell-writer"`, `workflowPhase`, `spawnDepth: 1`, `requestNonInteractive: true` | Resolve the run through that file, re-reading for ≤2 s |
| 4: plugin agents | `agentType: "nh:cell-writer"` resolves the plugin agent; it used only its listed tools. Writer PreToolUse payloads carry `agent_id`, `agent_type: "nh:cell-writer"`, the launching turn's **same `prompt_id`**, `effort: {level}` inherited from the session, `mcp_server: {name: "plugin:nh:nh"}` | No `model`/`effort` in agents or the script |
| 5: task notification | The completion fires UserPromptSubmit with a **new** `prompt_id`; the prompt is exactly one `<task-notification>` block holding `<task-id>`, `<tool-use-id>` (the launching Workflow call), `<output-file>`, `<status>completed</status>`, `<summary>`, `<result>{json}</result>`, `<diagnostics>`, `<usage>`; nothing outside it | Strict parser: whole prompt = complete blocks, each with `<task-id>` and `<status>`; other inner elements allowed. Alias it to the open human turn |
| 8: result delivery | The main agent received the full `<result>` JSON and replied from it | Keep the workflow result compact but complete |
| 9: `claude plugin validate --strict` | Passes for `plugins/nh` (with `agents/` and `workflows/`) and the marketplace root | — |
| TaskStop | `TaskStop {task_id}` answers `{message, task_id, task_type: "local_workflow", command}`; the stopped run sends no task notification in `-p` | PostToolUse `^TaskStop$` marks the run done with status `killed`; verified: the main conversation's write then passes (no E108) |
| End to end, after implementation (scratch copy of the sandbox, real JupyterLab, `NH_HEADLESS=1`) | `/nh:qa-cell <ask>`: the writer is bound to the run, adds one cell (`cell_added agent=nh:cell-writer`), QA passes it with `nh_inspect` only, and the report arrives as a `turn_alias` of the same human turn. `--settings '{"ultracode": true}'` with a plain message: the model launches `nh:qa-cell` by itself. A plain message at normal effort stays single-agent | — |
| 6, 7 | Not reached headlessly: background permission prompts need an interactive session, and QA never needed Read (the outputs were short) | Check in an interactive run; the launch guard covers `approve_before_run`, and QA falls back to `nh_inspect(view="cell")` if Read prompts. Answered under "v0.1.1 acceptance" |

## v0.1.1 acceptance (Claude Code 2.1.282)

Run on the final 0.1.1 code in a scratch copy of `dev/sandbox`, with the notebook trimmed to its title and a
real JupyterLab on 127.0.0.1. The headless runs used `NH_HEADLESS=1 ENABLE_CLAUDEAI_MCP_SERVERS=false
claude -p … --plugin-dir plugins/nh --allowedTools "Skill,Workflow,TaskStop,Read,mcp__plugin_nh_nh"`, and
the browser checks used a JupyterLab tab on the same server. The manual run (the last five rows) used an
interactive `claude` in a separate scratch project, with nh installed from GitHub at project scope and
`/effort ultracode`. macOS, 2026-09-28.

| Check | Result | Decision |
|---|---|---|
| qa-cell end to end | With `--settings '{"ultracode": true}'` and a plain "load sales.csv", the model launches `nh:qa-cell` by itself. The writer adds one cell (`cell_added agent=nh:cell-writer`). QA passes it with no revision, and the report arrives as a `turn_alias` of the same human turn. A second run with a follow-up question gave the same result | — |
| E107: the user writes while the workflow runs | A `--input-format stream-json` driver sent a second message as soon as the launching turn ended, about 6 s before the writer's `nh_add_cell`. The add was refused: "Not written: this nh:qa-cell run belongs to an earlier user message…" (E107). No cell was added. The report arrived as a `turn_alias` of the second turn, and the reply told the user the cell wasn't written and why | As designed. Turn-gate refusals are not logged, so `nhctl metrics summarize` doesn't count them (its `rejections` are linter rules only) |
| Undo after a qa-cell turn | `--resume <session> "undo"`: `nh_undo` with no cell id removed the qa-cell's note and code cell. One `cell_undone` was logged, and the notebook matched its state before that message. The next `nh_inspect` warned that the kernel still held `ax`, `fig` and `region_price` from the undone cell, and said how to rebuild | — |
| Background Bash notification | The prompt starts `sleep 5` with `run_in_background` (this needs `Bash(sleep:*)` allowed) and asks for a cell. The turn logged one `turn_open` and one `cell_added`. The task notification was logged as a `turn_alias`, and its reply wrote no cell | — |
| S4: gateway overhead | `harness_ms` over the 3 `cell_added` events: p50 70 ms, p95 97 ms (budget 700 ms) | — |
| S4: hooks | Timed as `/bin/sh nh-hook <event> [variant]` with a warm runtime, 20 runs per case. SessionStart, UserPromptSubmit, PreToolUse (nh, file write, notebook Read, notebook Bash, foreign MCP, Workflow) and PostToolUse TaskStop: p95 61–103 ms, max 122 ms (SessionStart). Read or Bash that doesn't touch a notebook exits on the shell fast path: p95 17–18 ms. An earlier run had one 266 ms SessionStart outlier (budget 300 ms) | — |
| S4: runtime build (`nh-sync`) | Check with the runtime present: about 22 ms. Build into an empty plugin-data dir: 0.68 s with a warm uv cache, 2.7 s with an empty one | — |
| S5: install | With `CLAUDE_CONFIG_DIR` set to a scratch dir, `claude plugin marketplace add <clone>` then `claude plugin install nh@notebook-harness`. The cached `nh/0.1.1/` keeps the exec bit on `bin/nhctl`, `hooks/nh-hook` and `libexec/*`. `nhctl doctor --json` runs from the cache: `ok: true`, with D120 until the first `nh-sync` and the D160 advisory | — |
| S1 m1: live cells | nh's note and code cells appear in the open tab as they are added, with no reload | — |
| S1 m2: edit in the UI | Editing an nh cell and saving keeps its id and `metadata.nh`. `nh_inspect` shows it as `agent*` | — |
| S1 m3: move, cut/paste, copy/paste | Move and cut/paste keep the id and `metadata.nh`. Copy/paste gives the copy a new id (a UUID) with the same `metadata.nh`, and nh treats it as the user's cell: `human+nh` | As designed (`normalize_uids`) |
| S1 m4: Restart Kernel and Run All | Ids and `metadata.nh` are unchanged on disk, and execution counts restart at 1 | — |
| S1 m5: close, wait 70 s, reopen | The room is cleaned, then dropped from memory 60 s later. On reopen it loads from the ystore and saves back with the same 5 cells, ids and `metadata.nh` | — |
| S1 m6: page reload | Same 5 cells, no duplicates, and the UI edit is kept | — |
| S1 m7: the file on disk | Every nh cell has `"nh"` metadata (`uid`, `pair_uid`, `role`, `turn_id`, `source_sha`…), and the notebook is nbformat 4.5 | — |
| Install from GitHub | `/plugin marketplace add dheocahyo/Notebook-Harness-Plugin` failed with "Permission denied (publickey)". Claude Code clones over SSH with no terminal to ask for a passphrase, and this machine's GitHub key has one, is used only through a `github.com-personal` host alias, and wasn't loaded in the SSH agent. After `ssh-add --apple-use-keychain`, adding the marketplace by the alias URL (`git@github.com-personal:dheocahyo/Notebook-Harness-Plugin.git`) and `/plugin install nh@notebook-harness` worked | No README change: git credentials aren't nh-specific |
| S6 #6, auto mode | No prompts. The auto-mode classifier checked the writer's `nh_add_cell` and approved it, and the workflow finished normally | — |
| S6 #6, default mode | `claude --permission-mode default`, answering each prompt with plain "Yes". The workflow agents have `requestNonInteractive: false` here (it is `true` under `-p`). The prompt for the writer's `nh_add_cell` reached the user while the workflow ran in the background: the call waited 11.9 s for a cell that ran in 0.0 s. After approval the workflow carried on and QA passed the cell. `nh_run` never came up, because `nh_add_cell` runs the cell itself | As designed: this is Claude Code's default mode asking for a tool that isn't allowed, not nh's `approve_before_run`. That setting's docs say QA doesn't start because "its agents can't ask you", which holds under `-p` only. v0.2: re-test `approve_before_run = true` with QA in an interactive session |
| S6 #7: QA reads `.nh/outputs` | In both runs the cell's output was longer than `[output] max_chars` (2,000), so the result ended with `[full output: .nh/outputs/<hash>.txt]` and QA Read that file. No prompt in either mode: auto mode's classifier doesn't check Read, and in default mode the Read took 43 ms | QA keeps using Read; the `nh_inspect` fallback isn't needed |
| Other default-mode prompts | Inferred from wait times, not reported separately: the `Workflow` launch (30.2 s, against 2.9 s in auto mode), and `nh_inspect` in the turn that delivers the workflow's report (24.4 s, against 0.06 s in the first turn, where loading the nh:notebook skill allows it) | v0.2 candidate: stop `nh_inspect` asking in the report turn, for example with an allow rule offered by `/nh:init` |
