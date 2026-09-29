# Notebook Harness v0.1: design and implementation contract

This file is the single source of truth for module boundaries. The product plan it implements is the approved plan (decisions D1–D12). Every module below lists:
- its owner files;
- its public functions, with exact signatures;
- the data shapes it exchanges with other modules.

If you change a contract, change it here first.

## 0. Conventions

**Gateway code** (`plugins/nh/server/src/nh_gateway/**`, except `_shared`):
- Python ≥ 3.11, async where it touches I/O.
- No third-party imports outside `backend/`, `exec/`, `app.py` and `tools/`. Allowed: `fastmcp`, `jupyter_nbmodel_client`, `jupyter_kernel_client`, `pycrdt`, `nbformat`, `PIL`, `filelock`, `requests`.

**Shared code** (`_shared/**`):
- Stdlib only, **Python 3.9 compatible**: `from __future__ import annotations`, no `match`, no `X | Y` at runtime.
- Imported by hooks and `nhctl`.

**Hooks and nhctl** (`plugins/nh/hooks/nh_hooks/**`, `plugins/nh/scripts/nhctl/**`):
- Stdlib + `_shared` only, Python 3.9 compatible.
- Every entry point starts with:
  ```python
  import os, sys

  HERE = os.path.dirname(os.path.realpath(__file__))
  ROOT = os.path.realpath(os.path.join(HERE, "..", ".."))  # plugins/nh
  sys.path[:0] = [HERE, os.path.join(ROOT, "server", "src")]
  ```

**Tests:**
- Tests live in the repo-root `tests/<layer>/`.
- Run them with `uv run --project plugins/nh/server pytest ../../../tests/<layer>` from `plugins/nh/server`, or `uv run --project plugins/nh/server --directory . pytest tests/...` from the repo root.
- Tests put `plugins/nh/server/src` on `sys.path` via `tests/conftest.py`.

**User-visible text:**
- Name cells by title and `[n]`. Never use `nh-` ids or line numbers.
- Tokens are never logged; `policy.errors.scrub` removes `token=`.

**One new cell per user message (E110):**
- It has exactly two exceptions, both enforced by the gateway (§6.0 c):
  - an approved batch (§6.3);
  - an approved re-run list (§6.5).
- Each needs the user's yes to the question nh asked (the one-shot grant, §6.0 b). Nothing else lifts E110.
- The batch is enforced from C6, the re-run list from C7. Until then E110 has no exception.

## 1. Already written (read these first)

| File | What it defines |
|---|---|
| `_shared/paths.py` | `find_project()`; `Layout(project)` with every `.nh/` path; `atomic_write_json`, `read_json`, `append_jsonl` |
| `_shared/stamp_spec.py` + `_shared/tool_defaults.py` | `stamp_key(tool, args)`, `stamp_filename(key, tool_use_id)`, `bare_tool_name`, `TOOL_PREFIX` |
| `_shared/text.py` | `count_words`, `normalize_title`, `normalize_bullet`, `split_notes`, `render_note(title, bullets, level)` |
| `_shared/tomlread.py` | `load(path)`, `loads(text)` (tomllib or a mini parser) |
| `_shared/patterns.py` | `shell_notebook_write(cmd)`, `CELL_NOTEBOOK_WRITES`, `CELL_PACKAGE_INSTALL`, `CELL_SEPARATORS` |
| `config.py` + `defaults.toml` | `load(project) -> Config`; `Config[section][key]`; `Config.rule(key)`; `Config.problems`; `ConfigCache` |
| `policy/errors.py` | `NhError(code, detail="", **fields)` (a `ToolError`); `CATALOGUE`; `scrub()` |
| `backend/base.py` | `NotebookBackend` protocol, `NotebookRef`, `CellView`, `NewCell`, `CellPatch`, `ExecResult`, `ErrorInfo`, `KernelStatus`, `Execution`, `CellConflict`, `CellMissing`, `ServerInfo`, `OutputSummary` |
| `meta.py` | `new_code_id`, `note_id`, `source_sha`, `normalize_source`, `code_metadata`, `note_metadata`, `nh_meta`, `is_agent_code`, `is_note` |

## 2. Probe payloads (kernel → gateway)

Probe sources live in `nh_gateway/probes/<name>.py`. Each is a plain Python script run in the **user's kernel** under these constraints:
- It sees a dict `_A` with its arguments.
- It must run on Python 3.10–3.13.
- It may import only the stdlib.
- It must bind no names in the user namespace.
- It ends with exactly one `display({"application/vnd.nh.probe+json": payload}, raw=True)`.

Payloads by probe:

**`attach`**
```json
{"python": [3, 12], "prefix": "/…/.venv", "executable": "/…/python", "cwd": "/…/notebooks", "exec_count": 17}
```

**`vars`**
- Arguments: `_A = {"names": [...] | null, "max_vars": 40, "budget_s": 0.8, "max_cells": 20000000}`.
  - `names=null` means every user variable.
  - Skipped: names starting with `_`, `In`, `Out`, `exit`, `quit`, `get_ipython`, modules, functions and classes.
- Payload:
```json
{"vars": {
   "df": {"kind": "DataFrame", "lib": "pandas", "shape": [10432, 8], "columns": ["a", ...], "dtypes": {"a": "float64", ...},
          "nulls": {"price": 312}, "mem": 667648},
   "s":  {"kind": "Series", "lib": "pandas", "len": 10, "dtype": "int64", "nulls": 0, "name": "x"},
   "X":  {"kind": "ndarray", "shape": [800, 12], "dtype": "float64"},
   "n":  {"kind": "scalar", "type": "int", "repr": "42"},
   "l":  {"kind": "container", "type": "list", "len": 3},
   "m":  {"kind": "object", "type": "sklearn.linear_model._base.LinearRegression"}},
 "truncated": false, "packages": {"pandas": "2.2.3", "numpy": null, "sklearn": "1.5.2"}}
```
- `packages` holds a version from `importlib.metadata`, or `null` when the package is installed but not imported. A package that is not installed is omitted.

**`var`**
- Arguments: `_A = {"name": "df", "rows": 5}`.
- Payload: `{"name": "df", "summary": {<same as the vars entry>}, "head": "<text table>", "text": "<reprlib repr for non-frames>"}`.

**Errors.** When a probe fails, the payload is `{"error": "<type>: <message>"}`.

## 3. Module contracts to implement

### 3.1 `lint/magics.py`, `lint/lint.py`, `dataflow.py`

**`magics.py`**
```python
@dataclass
class Masked:
    text: str                 # same number of lines; magic lines replaced by "pass" + spaces
    cell_magic: str | None    # "%%time" -> "time"; the whole cell then skips AST rules
    magic_lines: set[int]     # 1-based

def mask(source: str) -> Masked
```

**`lint.py`**
```python
@dataclass
class Issue:
    rule: str                 # "L004"
    key: str                  # "notes" (config key for hints; hard rules use their own key)
    severity: Literal["error", "hint"]
    message: str              # human sentence, quotes code, never line numbers
    fix: str                  # one sentence telling the agent what to change

@dataclass
class LintReport:
    errors: list[Issue]; hints: list[Issue]
    code_lines: int; comment_lines: int
    defs: set[str]; uses: set[str]; parsed: bool
    title: str | None; bullets: list[str]      # normalised values to write

def lint_cell(code: str, *, title: str | None, notes: list[str] | str | None, intent: str | None,
              cfg: Config, require_note: bool, require_intent: bool,
              kernel_python: tuple[int, int] | None, names_above: set[str] | None) -> LintReport
```

Hard rules (IDs are fixed):

| ID | Rule |
|---|---|
| L001 | empty code |
| L002 | cell separators (COMMENT tokens matching `patterns.CELL_SEPARATORS`) |
| L003 | title: > `title_max_words`, multi-line, or markdown/HTML (`#`, `<`, `` ` ``, `![`); required when `require_note` |
| L004 | notes: fewer than `notes_min` or more than `notes_max` bullets; a bullet > `bullet_max_words`; heading, fence, image, HTML or nested list inside a bullet |
| L005 | missing intent when `require_intent` |
| L007 | syntax error, only when `kernel_python` is known and ≤ the gateway's own version (`ast.parse(masked, feature_version=kernel_python)`); otherwise a hint |
| L008 `notebook_write` | notebook writes |
| L009 `package_install` | package installs |
| L010 `markdown_output` | `%%markdown`/`%%html` cell magics; `Markdown(`, `HTML(` or `Latex(` called with a string literal of more than `title_max_words` words |

Severity for L008–L010 comes from `cfg.rule(key)`, where `off` drops the rule.

Hints (keys as in `defaults.toml [lint.rules]`; `cfg["lint"]["mode"] == "strict"` promotes every hint to an error):

| IDs | Rules |
|---|---|
| L101–L109 | long_line, long_cell, deep_nesting, long_chain (**logical** links across lines > `max_chain`), comment_budget (`0` = no comments; else `max(1, code_lines // ratio)`), commented_out_code, multi_statement, star_import, bare_except |
| L111–L114 | non_idempotent, apply_lambda, cryptic_name (`df2`, `tmp`, `temp`, `data1`, single letters outside loops or comprehensions), early_def |
| L116–L119 | no_visible_output, many_outputs (≥ 3 display points), hidden_warnings, prose_print (> 15 words) |
| L120 | kernel_only_name: needs `names_above`; a use not defined above and not a builtin or IPython name |
| L122 | intent_too_long (> 200 chars) |
| L123 | long_bullet (> `bullet_hint_words`) |

**`dataflow.py`**
```python
def defs_uses(source: str) -> tuple[set[str], set[str], bool]   # (defs, uses, parsed); unparsable => (set(), {"*"}, False)
def downstream(cells: list[tuple[str, str]], start: int, tainted: set[str]) -> list[str]
    # cells = [(cell_id, source)] code cells in notebook order; walk cells[start:], return ids that use a tainted name;
    # their defs join tainted. A cell with uses {"*"} counts as using everything.
def defined_names(sources: list[str]) -> set[str]                # union of defs (for L120)
```
Defs include:
- assignment targets, including tuple unpacking, `for`, `with`-as and walrus;
- `import` aliases, `def`/`class`, `del`;
- mutation of a base name: `x[...] =`, `x.attr =`, augmented assignment, `x.method(..., inplace=True)`;
- **expression-statement method calls** `x.m(...)`, and names passed as arguments to a bare call statement.

**Tests:** `tests/unit/test_magics.py`, `test_lint_hard.py`, `test_lint_hints.py`, `test_lint_tokenize_compat.py`, `test_dataflow.py`.

### 3.2 `exec/shaping.py`, `render.py`

**`shaping.py`**
```python
@dataclass
class ShapedImage: data: bytes; mime: str; width: int; height: int; orig: tuple[int, int]
@dataclass
class Shaped:
    text: str                      # ≤ max_chars; sections per output, head+tail truncation, tracebacks keep the tail
    images: list[ShapedImage]      # ≤ max_images, each ≤ image_max_px on the long side (PNG; JPEG q80 if PNG > 150 KB)
    dropped_images: int
    truncated: bool
    full_path: str | None          # project-relative path of the untruncated copy in .nh/outputs/
    error: ErrorInfo | None        # from output_type == "error" (ANSI-stripped; line from "Cell In[n], line N" / "line N")
    summary: OutputSummary

def strip_ansi(text: str) -> str
def shape_outputs(outputs: list[dict], *, max_chars: int, max_images: int, image_max_px: int,
                  save_dir: Path | None) -> Shaped
def summarize_outputs(outputs: list[dict]) -> OutputSummary    # cheap: no base64 decoding
def redact(text: str) -> str               # FR-14: secrets.current().redact from v0.2 (§6.8)
def prune_outputs_dir(save_dir: Path, max_files: int = 200, max_bytes: int = 50 * 2**20) -> None
```
Mime preference: `error`, then `text/plain`, then `text/markdown`, `text/latex`, then images (`image/png`, `image/jpeg`), then `text/html`. HTML becomes text only when there is no `text/plain`; tables become TSV via `html.parser`. Then `application/json` (compact), Plotly as `[plotly figure: N traces]`, widgets as `[widget]`, SVG as `[SVG figure omitted]`. `\r` progress bars keep the text after the last `\r` on each line. Consecutive streams with the same name are coalesced.

**`render.py`**
```python
def cell_label(title: str | None, execution_count: int | None, source: str = "") -> str
    # '"Drop rows with missing price" [14]' ; falls back to the first code line (≤ 40 chars)
def selfcheck(before: dict, after: dict) -> tuple[list[str], list[str]]
    # (self_check_lines ≤ 8, check_this_lines) from two `vars` payload dicts
def next_block(status: str, *, retries_left: int, waits_left: int, cell: str) -> str
class Result:
    def __init__(self, first_line: str, machine: str): ...
    def section(self, name: str, lines: list[str] | str) -> "Result"   # skips empty sections
    def text(self) -> str
```
The `check this` heuristics:
- rows went to 0;
- more than 50% of rows removed;
- rows grew after `merge`/`join` (the code contains `merge(` or `.join(`);
- a new all-null column;
- shape unchanged although the code contains `drop`, `dropna` or a boolean filter `[...]`.

**Tests:** `tests/unit/test_shaping.py`, `test_render.py`.

### 3.3 `history.py`, `state.py`, `log.py`

**`history.py`**: append-only JSONL in `.nh/history/<uid>.jsonl`, plus an `_ops.jsonl` index.
```python
@dataclass
class Op:
    op_id: str; op: Literal["insert", "edit"]; ts: float; notebook: str; session_id: str; turn_id: str
    uid: str; note_uid: str | None; index: int
    before: dict | None     # edit: {"source","nh","tags","note_source","note_nh","execution_count"}; insert: None
    after_sha: str          # source_sha after the last attempt in the turn
    defs: list[str]; attempts: int; undone: bool

class HistoryStore:
    def __init__(self, layout: Layout): ...
    def record(self, *, op: str, notebook: str, session_id: str, turn_id: str, uid: str, note_uid: str | None,
               index: int, before: dict | None, after_source: str, defs: set[str]) -> Op
        # FOLDS: if an op for (turn_id, uid) exists, update after_sha/defs/attempts+1 and keep the ORIGINAL before/op
    def last_ops(self, notebook: str, session_id: str, turn_ids: list[str]) -> list[Op]   # newest first, not undone
    def ops_for(self, uid: str) -> list[Op]
    def mark_undone(self, op: Op, *, turn_id: str) -> None
```

**`state.py`**
```python
class StaleStore:     # .nh/state/stale.json {notebook: {uid: {"reason","by","turn_id","exec_count"}}}
    def mark(self, notebook, uids: list[str], *, reason, by, turn_id, exec_counts: dict[str, int | None]) -> None
    def clear(self, notebook, uids) -> None
    def get(self, notebook) -> dict[str, dict]
class DriftStore:     # .nh/state/kernel_drift.json {notebook: {"kernel_id","since","names":{name: why}}}
    def add(self, notebook, kernel_id, names: dict[str, str]) -> None
    def get(self, notebook) -> dict | None
    def clear(self, notebook, names: set[str] | None = None) -> None   # None = all
def write_last_cell(layout, **fields) -> None   # {v, session_id, notebook, cell_id, title, exec, status, turn_id, retries_left, finished_at}
def read_last_cell(layout) -> dict | None
```

**`log.py`**
```python
class EventLog:
    def __init__(self, layout: Layout, max_bytes: int = 5 * 2**20): ...
    def emit(self, event: str, **fields) -> None   # envelope {"v":1,"ts","event",...}; scrubs tokens; rotates to log.1.jsonl
```

**Tests:** `tests/unit/test_history.py`, `test_state.py`, `test_log.py`.

### 3.4 Backend: `backend/discovery.py`, `rest.py`, `rtc.py`, `kernel.py`; `exec/runner.py`, `exec/flusher.py`, `exec/probes.py`; `probes/*.py`; `backend/fake.py`

`RtcBackend(layout, cfg_cache)` implements `NotebookBackend` against a live JupyterLab, per plan §4.4. Key points:

**Discovery**
- `discover(project, cfg) -> ServerInfo`, raising `NhError` E130/E138.
- Order: env, config, `lab.json`, then a jpserver scan over four runtime dirs. Pid alive, `/api/status` with the token, reject jupyter_server < 2.
- `resolve_notebook(project, server, rel_or_none, cfg) -> NotebookRef`, raising E132.

**REST** (`requests`, run in a small dedicated `ThreadPoolExecutor`):
- sessions: GET/POST
- kernel model: GET `/api/kernels/{id}`
- `contents_exists`
- `collab_session` (PUT)
- `status`
- `extensions`
- **`NO_PROXY` is set before import.**

**RTC**
- `NhNbModelClient(NbModelClient)`: awareness messages are applied.
- `RtcDocument.ensure()`: a `run()` task plus `wait_until_synced()` with an 8 s cap, else E135. A reconnect bumps `generation`.
- **Inside `with nb._lock:` touch only `nb._doc.ycells`, `ycell[...]`, `nb._doc._ymeta` and `len(nb)`. Never a public `NotebookModel` method.**
- Insert = `create_ycell(nbformat.v4.new_*_cell(..., id=, metadata=))` + ONE `nb._doc._ydoc.transaction(origin=nb._changes_origin)`.
- Update = difflib opcodes applied right to left on the `Text` in one transaction, checking `base_source` first.

**Kernel**
- Attach per call: sessions lookup (UUID race wait, most connections), POST if missing (501 fallback).
- Clients: `JupyterKernelClient(server_url=, token=, kernel_id=, username="nh-agent")`. If `has_kernel` is false, raise E134. Assert `channels_running`. Stop with `stop(shutdown_kernel=False)`. **Two clients per kernel: exec and probe.**
- Do NOT use `execute_interactive`. Use our own request loop on the client's channels:
  1. flush with `client.iopub_channel.get_msgs()`;
  2. `msg_id = client.execute(code, silent=, store_history=, allow_stdin=False, stop_on_error=False)`;
  3. loop `client.iopub_channel.get_msg(timeout=0.25)`; skip messages whose `parent_header.msg_id` isn't ours; set `started` on our `status: busy` or `execute_input`;
  4. check `client.connection_ready`, the abort flag and the deadline on every iteration;
  5. stop on our `status: idle`;
  6. then read the shell reply with the same filter.

**Runner and flusher**
- `start_execution` runs that loop on a daemon `threading.Thread`, forwarding messages with `loop.call_soon_threadsafe`.
- The flusher applies `jupyter_kernel_client.client.output_hook` semantics to a local list, then every 200 ms writes changed outputs into the ycell **resolved by id** (sanitized and capped), with `execution_state` running/idle and `execution_count` from the reply.
- Lost detection: REST poll every 2 s.

**Probes**
- `exec/docsafe.py` (backend-owned; used by the flusher and FakeBackend):
  - `sanitize_for_doc(value) -> Any`: NaN/±Inf become None; ints outside int64 become str; lone surrogates become U+FFFD; applied recursively.
  - `cap_outputs_for_doc(outputs) -> list[dict]`: stream text keeps the last 1 MB; a mime bundle over 5 MB becomes a text notice; 8 MB per cell.
- `exec/probes.py`: `run_probe(client, name, args, timeout) -> dict` runs `exec(base64 source, {"_A": args})` with `silent=True, store_history=False`, and reads the `application/vnd.nh.probe+json` output.

**FakeBackend** (`backend/fake.py`)
- In-memory cells (nbformat dicts).
- Executes code with `exec` in one persistent namespace (`display` supported via a shim that appends `display_data`), producing nbformat outputs.
- Probes run the real probe sources against that namespace.
- Knobs: `kernel_busy`, `fail_open`, `exec_delay_s`, `python_version`.

**Tests**
- `tests/unit/test_discovery.py` (fake runtime dirs)
- `tests/integration/*` (real JupyterLab fixture in `tests/integration/conftest.py`)
- `tests/unit/test_fake_backend.py`

### 3.5 Turn gate and tools: `policy/stamps.py`, `policy/turn.py`, `tools/*.py`, `app.py`

**`stamps.py`**: `claim(layout, tool, raw_args, *, my_cc_pid, ttl) -> Stamp | None`, following plan §4.3.

**`turn.py`**: `TurnState`, `TurnLedger`, a per-notebook `asyncio.Lock` plus `filelock`.

**Tools:**
- `inspect.py`, `write.py` (add and edit), `run.py`, `undo.py`.
- Each is an async function taking `(services: Services, turn: TurnContext | None, **params) -> ToolResult`.

**`app.py`**: `create_server(project: Path | None, backend: NotebookBackend | None = None) -> FastMCP`, with:
- the instructions;
- the middleware from plan §4.3;
- tool `meta` from `_meta_rules.tool_meta(project, cfg)`.

**Tests:** `tests/gateway/*` use the real hook script to stamp calls (`tests/fakes/turns.py`).

### 3.6 Plugin packaging: hooks, libexec, nhctl, skills, evals

This is plan §§5–7:
- `plugins/nh/hooks/{hooks.json, nh-hook, nh_hooks/*.py}`
- `plugins/nh/libexec/{nh-mcp, nh-sync, nh-python}`
- `plugins/nh/bin/nhctl` + `plugins/nh/scripts/nhctl/*.py` + `_shared/scaffold/*`
- `plugins/nh/skills/**`
- `plugins/nh/agents/*.md`, `plugins/nh/workflows/qa-cell.js` (§3.7)
- `plugins/nh/evals/**`

**Tests:** `tests/hooks/*`, `tests/nhctl/*`.

### 3.7 Cell QA workflow (v0.1.1): `_shared/turn_record.py`, `hooks/nh_hooks/post_tool.py`, `agents/*.md`, `workflows/qa-cell.js`

Under ultracode (the model sees a system reminder), or on `/nh:qa-cell <ask>`, the main agent launches the `nh:qa-cell` workflow instead of writing. `nh:cell-writer` writes and runs the message's one cell; `nh:cell-qa` live-checks it (code, real output, self-check, kernel variables; it never runs code); on `revise` a new writer edits the same cell. The invariant is unchanged: at most one new or changed cell per human message, enforced by the gateway, failing closed. The skill side is `skills/notebook/reference/qa-workflow.md`.

**Turn record v2** (`.nh/state/turns/<session>.json`; the UserPromptSubmit hook is its only writer):
```python
{"v": 2, "session_id", "prompt_id",   # the latest prompt id seen
 "turn_id",                            # the human turn; None for an orphan
 "aliases": [...],                     # notification prompt ids; reset per human turn, at most 1000
 "human": bool, "ts",                  # when the turn opened
 "alias_ts",
 "earlier": {alias: turn_id}}          # aliases of earlier turns, at most 1000
```
- `turn_record.read()` reads a v1 record as `turn_id = prompt_id`. `canonical(record, pid)` maps the turn id or any alias to `turn_id`; an unknown `pid` comes back unchanged, so v0.1's E102 still applies. `opened()`, `aliased()` and `orphan()` build records. A new human turn moves the previous turn's aliases into `earlier`, so a late call from an earlier notification's prompt still counts against that message's budget (or gets E102).
- `notification_blocks(prompt)` is strict: the whole prompt must be complete `<task-notification>…</task-notification>` blocks, each with `<task-id>` and `<status>`. Other inner elements (`<tool-use-id>`, `<output-file>`, `<summary>`, `<result>`, `<diagnostics>`, `<usage>`) are allowed; any text outside the blocks makes it a human prompt. It returns `{task_id, tool_use_id, status}` per block.
- A notification fires UserPromptSubmit with a new `prompt_id`. The hook marks the runs it names done, then writes an alias of the open human turn, or an orphan (`turn_id: None`, so writes get E102) when none is open (session start, after `/clear`). The reminder says one of: the report of nh's own qa-cell run (`is_own_run`) for this message ("reply from it; write no new cell unless its writer wrote none"), such a report for an earlier message (report it, change no cell), or any other background task, a foreign workflow named qa-cell included (not a user message; write no new cell). A human prompt is unchanged: `RULE` is byte-identical and there is no effort-based nudge.
- `TurnContext.prompt_id` carries the canonical turn id, so the ledger, locks, metadata `turn_id`, history fold, `_rebuild_claims`, `last_cell`, events and undo recency need no other change. `stamps.claim` compares canonical ids; two fresh stamps with the same key and different `agent_id`, of the same turn and at most 60 s apart, are ambiguous (a leftover from an earlier message, such as a call rejected at its permission prompt, is not): all are moved to `stamps/ambiguous/`, and each of their calls within 60 s gets E101 with the detail "Two identical calls arrived together (one from a subagent)…" and "Next: Retry this call once."

**Runs file** (`.nh/state/workflows/<session>.json`, the last 20 runs; `stamps.gc_runs` deletes a session's whole runs file once it is untouched for 24 h):
- PostToolUse `^Workflow$` (`nh-hook post-tool workflow`, main conversation only) records each `async_launched` result: `{run_id, task_id, tool_use_id, name, launched_by, transcript_dir, turn_id (canonical), prompt_id, ts, done_ts: None, status: None}`. It makes no decision.
- The model calls `Workflow {name: "nh:qa-cell", args}`; `/nh:qa-cell <ask>` goes through the same tool call with `args` as the raw string. `tool_response.workflowName` is `qa-cell`; both names count.
- `launched_by`: PostToolUse's `tool_input` is `{name, args, script}`, with the resolved script text injected (for a `scriptPath` launch too). `"name"` when the sha256 of `script` (CRLF → LF, outer whitespace stripped) equals the same of the plugin's `workflows/qa-cell.js` and the launch guard saw it (launched as `nh:qa-cell`, by `scriptPath`, or inline with no name); `"script"` when the script differs or runs under another name (a copy among the project's workflows). With no script: `"name"` for `nh:qa-cell` and no `scriptPath`, else `"scriptPath"` or `"script"`. A resume (`resumeFromRunId`, same run id) stays `"name"` only when the resumed run was recorded as one. Only `"name"` runs may write.
- PostToolUse `^TaskStop$` (`nh-hook post-tool task-stop`): a stopped workflow sends no task notification, so the hook marks the run with the stopped `task_id` (`tool_response.task_id`, else `tool_input.task_id` or `shell_id`) done with status `killed`.
- Helpers: `record_run`, `find_runs`, `mark_done(tool_use_id, task_id=, status=)` (a notification names the run by its launching `tool_use_id` or its `task_id`; it sets `done_ts` and the notification's `status`), `open_runs(turn_id)` (open means no `done_ts` and under 1 h old), `run_open`, `is_own_run` and `run_for_agent(agent_id)`.
- PreToolUse `^Workflow$` (`nh-hook pre-tool workflow`; it sees only the call's own fields, not the resolved script) acts on nh's launches: `name` `nh:qa-cell` (a bare `qa-cell` names a project or user workflow, never the plugin's), nh's script inline or by a `scriptPath` whose file hashes to it (the launch result offers its saved copy), or a `resumeFromRunId` of one of nh's own runs. It denies such a launch from a subagent (only the main conversation launches it), when `[approval] approve_before_run = true` (the workflow can't ask the user; skipped when `NH_HEADLESS=1`, as the gateway's own approval check), or when this human turn already has an nh qa-cell run, open or done.

**Writer binding and the gate**
- `pre_tool.py` stamp: a call with an `agent_id` whose `agent_type` is not `nh:cell-writer` is denied (`SUBAGENT_REASON`); the writer's `nh_undo` is denied. Writer stamp bodies carry `agent_id` and `agent_type`; main-conversation bodies are unchanged. There is no SubagentStart hook.
- Writer payloads carry the launching turn's `prompt_id` and the session's effort, so neither identifies the run. By the writer's first PreToolUse, `<transcript_dir>/agent-<agent_id>.meta.json` exists with `{"agentType": "nh:cell-writer", "workflowPhase", "spawnDepth": 1, …}`. The gateway resolves the run as the one recorded run of this session whose `transcript_dir` holds that file, re-reading for up to 2 s (the PostToolUse race).
- Gate order: E105, E101 (no stamp, or an ambiguous one), E106 as before; `_writer_turn()` when `agent_id` is set; E104 (a writer's with `RETURN_TO_WORKFLOW`); E102 against the canonical turn (an orphan's with "A background task finished; no user message is open."); E108 for the main conversation only; then `TurnContext(..., agent_type, run_id)`.
- `_writer_turn` refusals (but E103 for an agent that isn't the writer) carry `next_step = RETURN_TO_WORKFLOW`: "Return this refusal to the workflow as your final answer; don't retry or reply to the user." Inside a tool, `_guarded` adds `WRITER_LINE` ("Writer: return this refusal to the workflow; don't retry or reply to the user.") after `Next:` to every writer refusal that doesn't already say that, except E120 (fix and call again). Writer E107, E110, E112 (no revisions left, or no OK run to revise), E141 and E144 use `RETURN_TO_WORKFLOW` as their Next line, so a writer never reads "ask the user" or "fix it with nh_edit_cell":

| Check | Refusal |
|---|---|
| `agent_type` is not `nh:cell-writer` | E103 (catalogue text) |
| `nh_undo` | E103 "- nh:cell-writer can't undo; only the main conversation can." |
| `nh_run` with a mode other than `wait` | E103 "- nh:cell-writer may only wait for its cell (mode="wait"); writing a cell runs it." |
| no run, or several, hold the agent's meta file | E103 "- This agent is not inside nh's qa-cell workflow: no nh:qa-cell run of this session, or more than one, lists it." |
| the run's name is not qa-cell, or `launched_by` is not `"name"` | E103 "- Only nh's own nh:qa-cell workflow, launched by name, may write; this run's script is not nh's." |
| the run is done, was launched over 1 h ago, or its `turn_id` is not the record's `turn_id` (the user wrote since) | **E107** |
| otherwise | the run's `turn_id` |

- **E108**: a main-conversation add, edit, `nh_run(mode="run")` or undo while an open qa-cell run belongs to this turn. `wait` and `interrupt` pass. First line "Not {verb}: the nh:qa-cell workflow is writing this message's cell."; Next "Tell the user it is still writing and checking the cell; reply when its report arrives. To change course, stop it first." A stopped workflow sends no notification; the PostToolUse `^TaskStop$` hook marks its run done, so after TaskStop the main conversation writes again. A run stopped from the task panel, or one that never reports, holds only its own message, for at most 1 h.
- **E107** (writers only): "Not {verb}: this nh:qa-cell run belongs to an earlier user message, already reported, or started over an hour ago."
- E103 now reads "Only the main conversation, or nh's cell writer inside the nh:qa-cell workflow, may change the notebook; other subagents are read-only."

**Revisions** (`[turn] max_revisions`, default 2)
- `TurnState.revisions` ({uid: n}) and `TurnState.writer_run`: the first run that writes owns the turn; any other run gets E110 "another nh:qa-cell run owns this message's cell".
- Writer `nh_edit_cell`: `base_sha` is ignored; a cell the user wrote is E144 and a user change since nh's write is E141, both terminal. On an `ok` cell the edit is a revision while `revisions[uid] < max_revisions`, else E112 with the detail "- No revisions left (n of max used; [turn] max_revisions)."; the main conversation keeps E112. RETRYABLE statuses share the normal retries; any other status is E112 for a writer, whose first line reads "{cell} has no OK run to revise and no failed run to retry".
- Every writer write sets `metadata.nh.agent` (the writer's agent type); a revision also writes `metadata.nh.revision = {"turn": turn_id, "n": k}`; the rollback restores both, and `_rebuild_claims` rebuilds `revisions` from them. The writer's add and its revisions share `(uid, notebook, canonical turn)`, so history folds them into one op and one undo restores the state from before the message.
- Writer results only: the machine line gains ` revisions=n/max`, `render.next_block(..., audience="writer")` says to return the whole result to the workflow, and `nh_run(mode="wait")` with nothing running says to return the last result to the workflow instead of waiting for the user. Main-conversation results stay byte-identical.

**Workflow and agents**
- `workflows/qa-cell.js`: `meta.name` is `qa-cell`. `args` is the ask as a string, or `{ask, context, cell, notebook}`; an empty ask returns `no_ask` without spawning an agent. Writer, then QA, then revise-and-QA rounds while QA says `revise` with a blocker or major finding and the writer's `revisions=n/max` has room (`MAX_QA_ROUNDS = 5` is only a safety cap). QA verdicts: `pass`, `revise`, `fail`, `unchecked`.
- It returns data only: `{outcome, status, cell{title, exec, notebook}, result, changes[], revisions, qa{final_version_checked, verdict, summary, open_findings, earlier_findings, numbers_checked, rounds}, lead_lines, notes}`. `result` drops nh's `--- next ---` section and a refusal's `Next:`/`Writer:` lines. `qa.final_version_checked` is true only when QA checked the last version written with a verdict other than `unchecked`. When QA never saw that version, `verdict` is `unchecked` and its findings move to `earlier_findings`. `outcome`: `checked`, `not_checked`, `not_written`, `refused`, `writer_failed` or `no_ask`.
- `agents/cell-writer.md` (tools `nh_inspect`, `nh_add_cell`, `nh_edit_cell`, `nh_run`) and `agents/cell-qa.md` (`nh_inspect`, `Read`), both with `skills: [nh:notebook]`. Neither the agents, the script nor the skills set `model` or `effort`: the session's own apply.

**Events**: `turn_alias` (`session_id`, `turn_id`, `prompt_id`, `prompt_chars`, `tasks`: the notified task ids) replaces `turn_open` for notifications, so `nhctl metrics` doesn't count them as turns. `cell_added` and `cell_edited` gain `agent` (the writer's agent type, or null) and `revision` (the revision number, or null).

**Tests:** `tests/unit/test_turn_record.py`, `tests/gateway/test_qa_workflow.py`, the hook tests, the `qa-cell.js` stub harness in `tests/unit/test_qa_cell_workflow.py` (it also checks the stubs against the gateway's real machine line, writer Next block and refusals), and `tests/unit/test_skill_files.py` for the agents, the workflow and the skill texts.

## 4. Deviations from the PRD (v0.1)

- **No Datalayer jupyter-mcp-server proxy.** Its 2.2.x releases depend on non-open "Datalayer License" packages, and its insert tool can't write metadata. We use Datalayer's BSD client libraries directly.
- **JupyterLab ≥ 4.6 with jupyter-collaboration ≥ 5 only.** VS Code notebooks are unsupported; the seam is `NotebookBackend`.
- **Lint runs in the gateway before writing, not in a PostToolUse hook.** Readability hints arrive with the result, after the run. Only the hard rules block before it.
- **No ≤ 3-line chat reply rule.** The user asked for budgets on notebook content only.
- **The ≤ 8-word header becomes a note** (title ≤ 8 words plus 2–5 bullets). The PRD's "median prose ≤ 25 words/cell" metric becomes words per note and per bullet.
- **`nh_edit_cell` is added**, for user-requested changes and same-turn retries.
- **A turn ends when the next user prompt arrives**, not on Stop, which doesn't fire on interrupts.

## 5. Contract changes made during implementation

These supersede the sections above where they differ.

| Area | Change |
|---|---|
| `NotebookBackend` | Adds `resolve_notebook(rel)`, `describe(ref)` (status lines, no tokens) and `take_notices(ref)` (one-time notes such as "started a kernel session" or "upgraded to nbformat 4.5"). `RtcBackend` lives in `backend/rtc_backend.py` and is re-exported from `backend/rtc.py`. `kernel_status()` may create a kernel session; `probe()` never does. |
| `exec/docsafe.py` | Backend-owned. `sanitize_for_doc` turns ints beyond ±(2⁵³−1) into strings, because Yjs stores them as BigInt, which JupyterLab can't serialize. |
| `render.selfcheck` | `selfcheck(before, after, *, code="")`. The merge/join and drop/filter checks need the cell source. `Result.text()` orders sections as in plan §4.2; unknown sections go just before `next`. |
| `history` | Folds on `(notebook, turn_id, uid)`. `ops_for(uid)` returns oldest first. `record()` raises `OSError` on disk failure, and the tools then warn that undo is unavailable for the cell. |
| `state` | `StaleStore(layout)` and `DriftStore(layout)` use `state.file_lock` (fcntl). `last_cell.json` `status` can be `ok`, `error`, `running`, `aborted`, `timeout`, `interrupted`, `lost` or `undone`. |
| Events (`.nh/log.jsonl`, epoch-seconds `ts`) | `turn_open` comes from the UserPromptSubmit hook, with `prompt_chars` (and since v0.2 `mode`, `request`, `answer`); a task notification logs `turn_alias` instead (§3.7), a message typed mid-turn `turn_absorbed`, and a classifier failure `classify_failed` (6.1). The gateway writes: `cell_added` and `cell_edited` (fields `note_words`, `bullet_words[]`, `code_lines`, `comment_lines`, `hints[]`, `exec{status,ms,exec_count}`, `harness_ms`, `source_sha`, and since v0.1.1 `agent` and `revision`); `cell_rerun`; `cell_rejected` (field `reason`, a list of rule ids); `cell_undone`; `cell_review` (fields `unedited`, `deleted`; written once per earlier nh cell, at the first write of a new message); and `history_failed`. `nhctl` writes `fresh_run` (fields `nb`, `ok`, `failing_cell_uid`, `n_cells`, `ms`, `via`). |
| Tool metadata | `_meta_rules.tool_meta(project, approve_before_run)`. `plugins/nh/server/tools.snapshot.json` and `evals/mocks/nh/_tools.json` are generated by `scripts/dump_tools.py`, and `tests/contract/test_tool_contract.py` fails when they are stale. |
| Hooks | Python runs with `-I -S -X pycache_prefix=$CLAUDE_PLUGIN_DATA/pycache` because the system 3.9 ships no bytecode. `nh-sync --path` prints the versioned venv. `NH_PYTHON` overrides the interpreter (tests). |
| `nhctl` | Has its own problem codes (`D1xx`). `env.json` is `{manager, prefix, python, jupyterlab, jupyter_collaboration, synced_at}`. `lab.json` is `{pid, url, runtime_file, env_prefix, notebook, started_at}` and never holds a token. `scaffold --add-dev-deps` applies the dev-dependency diff to an existing env file after the user says yes. |
| Linter | `dataflow.defined_names` may return `"*"` (star import, `%run`, `exec`), which disables L120. Hard-rule keys: `empty_code`, `cell_separator`, `title`, `notes`, `intent`, `syntax`, `notebook_write`, `package_install`, `markdown_output`. |
| pytest | One config, in the repo-root `pytest.ini`. Run with `uv run --project plugins/nh/server pytest`, adding `-m integration` for the real-JupyterLab tests. |


### 5.1 Changes after the adversarial review

| Area | Change |
|---|---|
| Turn budget | `NotebookLocks.hold(notebook, turn)` serializes each turn first and each notebook second. The one-cell check and the claim can't race across notebooks. The file lock is polled, so a cancelled call can't leak it. |
| Running cells | Nothing is written while nh runs a cell in the notebook's kernel (E133, before any write). If `start_execution` fails after a write, the write is rolled back and the claim released. `last_cell.json` is written with status `running`/`queued` when a call returns before the cell finishes. Finished runs stay in `Services.finished` until reported, so `nh_run(mode="wait")` after the finish still shows the output. |
| Statuses | `queued`: not started, waiting behind another cell. `deleted`: the user removed the cell while it ran. `interrupted` is no longer retryable: the user stopped it, so nh asks first. `last_cell.json` `status` can also be `queued`, `deleted` (also written when undo finds nh's cell already deleted) and `conflict` (the user typed into nh's cell before it started, so it did not run). The UserPromptSubmit reminder has a text for each (`STATUS_TEXT`): `queued` is "QUEUED behind a running cell; add nothing", `interrupted` says to ask before re-running or changing it, `deleted` "a no; don't re-add it", `conflict` "not run (the user typed into it first; ask before running it)". The notebook skill, `reference/tools.md`, `reference/replies.md` and instruction 7 treat `queued` like RUNNING and say what to do for `interrupted` (ask first, E117), `deleted`, `lost` and `conflict`. |
| Copies | `normalize_uids(cells)`: a copy of an nh cell (same `metadata.nh.uid`, new id) is treated as the user's own cell with `copied_from`. |
| User edits | The E141 check also applies to same-turn retries. Editing a cell the user wrote requires `base_sha` (**E144**). Notes carry their own `source_sha`: nh refuses to overwrite a note the user edited, and undo keeps it. E141 diffs against the code nh last wrote (`Op.after_source`). Undo refusals say "Not undone". |
| Notes on edit | Only `title` or only `notes` fills the other half from the existing note. A code-only edit adds a "Note unchanged" notice. The original `intent` is kept, and later asks go to `metadata.nh.edit_intents[]`. |
| Kernel identity | `KernelStatus.incarnation` changes when the kernel process restarts. The gateway records `kernel_id:incarnation` per notebook in `.nh/state/kernels.json`. A change clears `kernel_drift.json` for the notebook and adds a lead line "NEW kernel: earlier variables are gone…". A kernel prefix outside `env.json`'s prefix gives a line in `--- kernel ---`. |
| Drift | Undo records only names the kernel still holds (vars probe). Every before-probe and every inspect overview/vars view asks about the drift names and drops those gone. Drift lines are lead lines (`Result.lead`), above the first line. |
| Errors | `NhError(code, detail, next_step=…, **fields)`. `{detail}` fills the first line when the head has the slot. `cell_id` goes on the machine line, never the human line. New codes: **E144** (a human cell needs `base_sha`), **E145** (a markdown cell). E142 is a normal result, not an error: nothing is undone, the cell is named, and the next undo moves on. E110 points at `nh_edit_cell` when this message's cell failed. E120 has its own Next lines for L009 (ask first) and L002 (first step only). |
| Server URLs | `discovery.clean_url(url)` rebuilds every Jupyter URL as `scheme://host[:port]/path` (no user info, query or fragment) and returns None for a URL with a backslash, whitespace, a control or non-ASCII character, a host that isn't a plain DNS name or IP address, or a host that `urllib.parse` and urllib3 (what `requests` sends through) read differently (`http://evil.example\@127.0.0.1:8888/`). `is_loopback`, `local_url`, `_same_server` and the `[jupyter].url` check all go through it, and `check()` puts the rebuilt URL into `ServerInfo.url`, the only form REST calls and websockets use. A runtime file's token is lent (`_token_for`) only to exactly its own host and port. An unusable URL gives "…isn't a plain http(s) address…" (E130 detail), or "harness.toml [jupyter].url is not a plain URL, so nh ignored it" in status. |
| Data URLs | `split_secret_url` sends a data URL to `.env` as `DATA_URL` when it has user info, a query or a fragment, an `@` anywhere in its path, or a path segment that looks like a token (a `:`, a UUID, or a piece between `-_.~` of 16+ characters mixing letters and digits, or of 24+). The rule is the redactor's (`secrets.split_url`, `url_token_like`), so such a `DATA_URL` is also hidden from Claude whole (§6.8). The committed record is `scheme://host[:port]/path` with those segments shown as `…`. For database URLs (SQLAlchemy reads a password up to its `@`, so it may hold `/`, `?` or `#`), everything up to the last `@` is cut, and the record starts `scheme://…@` when the cut text held one of those. Scaffold's ignore lines add `.jupyter/` (jupyter-collaboration writes `.jupyter/collaboration_sessions.json` into the server root). |
| `nhctl lab` | `lab start` adopts a live JupyterLab the user started that already serves the project instead of starting a second one: `lab.json` then has `"adopted": true` and `env_prefix: null`. `lab start` `status` is `started`, `running` or `adopted`, and `log` is the command that shows the log's tail, scrubbed (`nhctl lab status --log`; the raw `.nh/logs/jupyterlab.log` may hold kernels' output, §6.8), or null when adopted (the init skill treats `adopted` like `running`). `lab status` adds `registered`, and with `--log` a `log_tail` (the last 40 lines, scrubbed). `lab stop` on an adopted server whose root is a parent folder returns `{stopped: false, adopted: true, root_dir}` and leaves it running. |
| `nhctl` reports | doctor's `jupyter.servers[]` adds `usable`, and its `lab` may include `registered: false`. The scaffold report adds `data.path_from_notebook` (the data path as the kernel sees it from the notebook). `metrics` `by_reason` is keyed by rule id. |
| Hook matchers | Files: `^(Write\|Edit\|MultiEdit\|NotebookEdit)$` (variant `file`, no text filter: Python always checks and resolves symlinks) and `^Read$` (variant `read`, keeps the shim's cheap text filter). Foreign MCP tools: `^mcp__(?!plugin_nh_nh__).+__.+$`, and Python decides, case-insensitively with camelCase split into words: a tool counts when its name holds notebook, kernel, ipynb, ipython or jupyter, its server's name holds jupyter or ipython, or a word starts or ends with "cell"; a first word list/read/get/describe/search/find/inspect/show/view is allowed; exec/execute/run counts only with code, python, python3 or snippet. In shell commands only nhctl's own words are exempt: its redirections and any command inside `$(...)`, `(...)` or backticks are still checked. |
| SessionStart | When `lab.json` is missing or its server is gone, the hook scans the gateway's four runtime dirs (stdlib only) for a live jupyter_server ≥ 2 whose root contains the project, in the gateway's order (root equal to the project, deepest root, newest file), and never prints the token. Otherwise it says "JupyterLab: no running server found for this project. Before notebook work, call mcp__plugin_nh_nh__nh_inspect(view="status"); only if it reports no JupyterLab (E130), ask the user whether to start it, then run `nhctl lab start`." Setup and SessionStart skip nh-sync (and SessionStart the "installing… /mcp" line) when `CLAUDE_CODE_EVAL_CONFINED` is set and not "0"/"false". |
| Other | `[stale] mark = "clear-count"` blanks downstream counts. `nh_inspect(rows=None)` uses `[inspect].head_rows`. Read-only calls don't change the active notebook. Results name the notebook when it isn't the configured one. `cell_rejected.reason` is a list of rule ids. |


### 5.2 Changes after the second verification round

| Area | Change |
|---|---|
| Turn state | `TurnState.claim_notebooks` (uid → notebook) records where each claim lives; refusals label this message's cell from its own notebook and name that notebook ("… [1] in notebooks/eda.ipynb"). `TurnState.undo_next` holds the step an "already deleted" undo offered. |
| Refusal texts | E110: "one new cell per message, and this message's cell is {cell}". E114 no longer names a cell. New **E117** "Not {verb}: {cell} was stopped before it finished, so nh asks before changing or re-running it", used by `nh_edit_cell` and `nh_run` on a cell the user interrupted (E112 "already ran OK" is only for cells that ran ok). |
| Cell labels | An untitled cell is ``the cell `<first code line, ≤40 chars>` [n]`` (no nested quotes); `next_block` capitalises a label that opens a sentence. Outline note rows show the heading unescaped (`$`, not `\$`). |
| Edit writes | `nh_edit_cell` writes source and metadata only; the run clears outputs and `[n]` when it starts (`begin_run`). A rollback after a failed start therefore leaves the previous outputs and `[n]` in place. |
| Conflict at start | When the user types into nh's cell between the write and the run (`CellConflict`), the history op is still recorded and `last_cell.json` gets status `conflict`, so undo targets that cell (E141 without `force`, because the user changed it). |
| Stale `base_sha` | A `base_sha` (or metadata `source_sha`) that doesn't match is accepted while the cell holds nh's last write, or what an undo of that write restored (`write.nh_wrote`). E141 is raised only for a real user change, always with its diff. |
| Already deleted (E142 result) | The leftover reason reads "from the deleted {name}"; `last_cell.json` gets status `deleted`. The Next line asks whether to undo the step before; a plain `nh_undo()` in the same message or the user's next one undoes exactly that step, even outside the 3-turn window. The offer lapses after that next message and is cleared once used. |
| Title/notes-only edit undo | When the restored code equals the current code, undo restores title, notes and metadata only: outputs and `[n]` stay, nothing is marked stale and no Kernel ≠ notebook drift is added ("Restored the previous title and notes of …; its code was unchanged"). |
| First lines and notices | The error first line ends with "." unless the message was cut ("…"). "Note unchanged" quotes `metadata.nh.rationale[0]` (raw) clipped to 80 characters and is skipped on same-message retries of the cell that failed. |
| Self-check | Names with no visible change are listed as "same shape and nulls: …" (frames, series), "same type: …" or "unchanged: …". The vars probe adds `sums` (a crc32 fingerprint of numeric column sums) for frames and series within `max_cells`, and the self-check says "…; values changed" when only values differ. `RunRecord.probe_names` keeps the cell's uses and defs, so `nh_run(mode="wait")` lists variables a long cell created. |
| Check this | Row filters are read with `ast`: `.drop*`/`.query`/`.filter` calls, or a subscript keyed by a condition, a `~`/`&`/`\|` expression, a call, or a name the probe lists as a Series or array. Column names and column lists don't count. A new name "lost rows" only when it keeps the source's columns and isn't an aggregation or reshape (`df.loc[mask, "date"]` is a look, not a loss). Merge growth compares with the primary (left) frame. The headline prefers the name the last line shows, else the last one bound. |
| Stream outputs | Stream text written to the notebook has no `\r` or `\b` (progress lines are resolved first), so JupyterLab's local copy and the shared text stay identical. Only the last output grows in place; a changed earlier output is rewritten together with every output after it (JupyterLab re-appends replaced outputs at the end). When the outputs shrink (`clear_output(wait=True)`) or the first rewritten output is a stream, every output is rewritten: JupyterLab keeps the name of the last stream it added, even a deleted one, and merges a new stream of that name into the output before it. The final flush trims only the last output to exactly its cap; an earlier stream may keep up to cap + `STREAM_SLACK_CHARS`. |
| Interrupts | `interrupted` means a KeyboardInterrupt after nh's own interrupt, or one from SIGINT (JupyterLab's stop): its chained traceback holds SIGINT's KeyboardInterrupt, which has no message (a cell that catches the stop and raises again with a message), or it has no message itself and its last frame is not a `raise KeyboardInterrupt` line in a cell. A KeyboardInterrupt the code raises itself, with or without a message, is an ordinary `error`. |
| Kernel identity | `kernel_status(ref, *, create=True)` in the protocol and every backend; `create=False` never starts a session. `KernelStatus.incarnation=None` means unknown (kernel busy, attach probe skipped or failed), never a new kernel: "NEW kernel" means the `kernel_id` changed, or both incarnations are known and differ. A known identity in `kernels.json` is never overwritten by an unknown one. `RtcBackend.kernel_status` uses the double-checked `_busy()` before skipping the attach probe. Every `nh_inspect` view except `status` runs the same check (with `create=False`). |
| Lead lines on refusals | `kernel_notes` also records the call's lead lines in the ContextVar `common.CALL_LEAD`, which `app._guarded` sets per call; any refusal (including E199) is prefixed with them. The call that detects a new kernel always carries the notice, whatever its outcome, and no later call repeats it. |
| Drift | `kernel_drift.json` entries gain `exec_counts` ({name: the notebook's highest execution count at undo}); a code cell with a higher count whose run did not fail and that binds the name afresh (not `df = df.x()`, `+=` or a mutation: `dataflow` fresh bindings minus uses) means the user re-ran it, and the name is cleared. `names` is oldest first; a name recorded again moves to the end. Before-probes ask about drift names in their own probe (`drift_args`, never cut by `max_vars`); `prune_drift` drops only names it sent, and nothing on a truncated payload. Undo's leftover probe uses `drift_args` too. The lead groups names by reason, newest first, at most 6, then "+N more"; the reminder says "(as of nh's last check)". |
| Foreign running records | `live_run` trusts another process's `running/<cell>.json` only while the notebook marks that cell running, the record is under 5 s old (`RECORD_GRACE_S`), or nh has no synced copy of the notebook; otherwise the record is deleted. |
| Refusals (round 3) | New **E118** "Not {verb}: the user typed into {cell} before it ran, so nh asks before running or changing it", for `nh_run`/`nh_edit_cell` on a cell whose status is `conflict`. E133 on a queued nh cell reads "The kernel is busy with another cell; X is queued behind it" with a "has not started" Next line. |
| What nh last left | `write.nh_last_source` is nh's newest write still in place, or what the oldest of the undos since then restored (undos are newest first per cell). The stale-`base_sha` acceptance and E141's "the user changed" diff both compare with it. |
| Already deleted, round 3 | Every not-undone op of the deleted cell is marked undone, and the offered "step before" skips that cell's own older steps, looking back over every message the ledger remembers (5) when the 3-message window has none. The offer carries its notebook (`TurnState.undo_next_notebook`), so a plain `nh_undo()` follows it there; the first line names a non-default notebook. An untitled cell reads ``the cell `…` `` there and "from the undone cell `…` [n]" in leftovers. Undoing a title/notes-only edit whose note the user rewrote reports "Nothing to restore … nh kept theirs". |
| Self-check, round 3 | Closing labels say what was compared: "same shape and nulls" only when both summaries carry null counts, "same shape" otherwise (frames over `max_cells`, arrays), "same length" for containers. "N% removed/lost" is floored and at most 99% while rows remain. A frame the cell binds again after a name is that name's source only as it was before the run (else no origin). A Series taken from a frame by an explicit `.drop*`/`.query`/`.filter` call counts as rows lost. The vars probe shows bools as True/False. |
| Discovery, round 3 | A loopback `[jupyter].url` spelled with another loopback name than a scanned runtime file (127.0.0.1 vs localhost, same port and base path) uses that file's own URL and token, so the token still goes only to its exact host. A 401/403 when nh sent no token says "needs a token nh doesn't have (set NH_JUPYTER_TOKEN, or remove [jupyter].url …)". Host names may contain "_". `nh_inspect(view="status")` adds a `reason` line with the E130 details. |
| Scaffold, round 3 | When `.env` is tracked by git, a secret data URL is not written there: the report lists `.env` as `skipped`, `data.secret_in_env` is false, and a warning says to `git rm --cached .env` and run /nh:init again. |

## 6. v0.2 (FR-9..FR-14)

v0.2 adds explain mode (FR-9), /nh:plan with an approved batch (FR-10), /nh:review (FR-11), guardrails that ask before risky steps (FR-12), junior/senior presets (FR-13) and secrets hygiene (FR-14), plus the v0.1.1 carry-overs (§6.12; a7 and a8 in §6.13) and two additions the user asked for: `nhctl fresh-run --via server` (§6.11) and drift issue filing (§6.13).
The decisions of the approved v0.2 build plan (D0–D13) are binding; this section records them as contracts.
§6.0 is the contract every chunk shares. Each of §6.1–§6.13 is written by the chunk that implements it, before that chunk's code.
Paths: gateway files are relative to `nh_gateway/`; `hooks/`, `scripts/nhctl/` and `skills/` are under `plugins/nh/`.

| Section | Mechanism | Plan | Chunk |
|---|---|---|---|
| 6.1 | Turn record, classifier, ledger v2 and fail-closed E102 | D0 a–b, D1 | C1 |
| 6.2 | Explain-only turns | D2 | C2 |
| 6.3 | /nh:plan and the approved batch | D3 | C6 |
| 6.4 | Cell approvals: package install, network, outside writes | D4 | C5 |
| 6.5 | Multi re-run | D5 | C7 |
| 6.6 | Native dialog hooks | D6 | C8 |
| 6.7 | Secret lint rules (L011, L014) | D7 | C4 |
| 6.8 | Redaction of what Claude sees, and E125 | D8 | C3 |
| 6.9 | Junior/senior presets | D9 | C9 |
| 6.10 | /nh:review | D10 | C10 |
| 6.11 | `nhctl fresh-run --via server` | D11 | C11 |
| 6.12 | nh_inspect allow rule and `approve_before_run` | D12 | C11 |
| 6.13 | a7, a8 and drift issue filing | D13 | C12 |

### 6.0 Shared contract

**a. Turn record fields and the classifier** (D0 a)
- `hooks/nh_hooks/prompt_submit.py` `handle` passes every human message to a new pure module, `_shared/intent.py` (Python 3.9, stdlib, shared by hooks and gateway). The result goes into the turn record (`_shared/turn_record.py`: `opened()` and the `read` whitelist) as optional fields:

| Field | Values |
|---|---|
| `mode` | `explain` \| `plan` \| `ask` \| None |
| `request` | `{batch, n}` \| `{rerun_stale}` \| None |
| `answer` | `yes` \| `no` \| None |
| `prev_turn_id` | defined in 6.1 |
| `prev_request` | defined in 6.1 |

- `RECORD_VERSION` stays 2. Absent keys read as None, `aliased()` keeps the fields and `orphan()` clears them.
- The classifier matches whole words, case-insensitive:

| Result | Message |
|---|---|
| mode `explain` | first word "explain", or starts with "/nh:explain"; and no change verb (fix, change, add, update, rewrite, refactor, make). "explain the fixed-width parse" is explain; "explain and fix" is not |
| mode `plan` | starts with "/nh:plan" |
| request `{batch, n}`, mode `ask` | "run (the) next N" or "run steps a-b", with n ≥ 2 |
| request `{rerun_stale}`, mode `ask` | "re-run (all) (the) stale (cells)" |
| `answer` | the whole trimmed message matches a small yes set or no set (defined in 6.1); "go" is yes only when it is the whole message |

- On a classifier exception the fields are None, the record is still written, and a `classify_failed` event is logged. Prompt text is never stored.
- A message absorbed mid-turn resubmits the running turn's prompt_id (V16). It can only tighten (user decision, 2026-09-29): its mode (explain, plan, ask) applies for the rest of the turn; its batch or re-run-stale request is kept for the next reply; its yes/no is never an answer; `prev_turn_id`, `prev_request` and `pending` stay as they are; it gets no new cell budget and `RULE` isn't re-injected (6.1).

**b. Pending question and one-shot grant** (D0 b, in the gateway)
- **Ledger v2.** `policy/turn.py` `TurnLedger` is stored as `{"v": 2, "turns", "pending"}` and still loads v1. `pending` is `{kind: cell|rerun, key: sha256(raw text), turn_id, ts}`: hashes only, never the question. A cell's key is defined in 6.4, a re-run's (`sha(notebook|ids)`) in 6.5.
- **First ask wins.** One question at a time: a second ask in the same turn gets E122 "already waiting for the user's answer".
- **Clearing.** A successful gated write in that turn clears `pending`.
- **Next human turn.** If `answer == "yes"` and `pending.turn_id == prev_turn_id`, the call with the exact matching key is accepted once. Any other answer drops `pending`.
- **Batch and re-run-stale requests** are granted when `answer == "yes"` and `prev_request` is set. The grant lives in new `TurnState` fields:

| Field | Holds |
|---|---|
| `batch_total` | `min(n, [turn] max_batch)`, set by the first gated call of the grant turn (6.3) |
| `batch_stop` | set by `report_run` on status != ok (running and queued included), a non-empty `check_this`, or any E12x; after that, new cells and retries get E123 (6.3) |
| `rerun_plan` | the approved call's `then`; each accepted `nh_run(cell_id=rerun_plan[0])` pops the head (6.5) |
| `rerun_stop` | set by a non-ok status or `check_this`; later re-runs get E123 (6.5) |
| `rerun_stale` | the granted "re-run the stale cells" request (defined in 6.5) |

- **Locking.** Every grant check and slot use happens inside `svc.locks.hold`.
- **Headless** (`-p` / `NH_HEADLESS`): no yes can arrive, so E122 stands and the gate fails closed (6.4).
- **Trust boundary.** The gateway trusts the hook-written record against a model that follows instructions; it is not an adversarial boundary. `file_guard` already denies `.nh/`, and `SHELL_NH_STATE_WRITE` (`_shared/patterns.py`) gains a Python `open('.nh/…','w'|'a')` pattern (6.6).

**c. E110 exceptions** (D0 c). E110 gets exactly two explicit, gateway-enforced exceptions, each needing a grant from (b): an approved batch (6.3) and an approved re-run list (6.5). §0 states them from C2 on (done).

**d. New codes** (D0 d; all grep-verified free at HEAD). Each gets a `docs/troubleshooting.md` row in its chunk.

| Code | Meaning |
|---|---|
| E109 | No notebook change in an explain, plan or ask turn |
| E122 | Needs the user's yes; Next = the exact question to ask |
| E123 | Approved batch or re-run stopped at step k; report and wait |
| E124 | Re-run list or order mismatch |
| E125 | Code contains nh's `[redacted:` marker |
| L011 | secret_print (hard) |
| L012 | network (ask) |
| L013 | outside_write (ask) |
| L014 | secret_name (hint) |
| D153 | Review timed out; partial report |
| D154 | Flagged cells need a yes (exit 2) |
| D155 | `--via server`: no JupyterLab |
| D156 | Review kernel died |
| D162 | nh_inspect allow rule missing |
| D171 | Unknown preset level |
| D172 | harness.toml not safely editable |

The existing E102 gains the detail "nh missed this message; send it again" (6.1).

**e. Config** (D9, D3, D4, D6)
- **Precedence:** defaults < preset overlay < explicit `harness.toml` keys < `NH_*`.
- **Preset overlay:** `comment_ratio` 8 for junior (today's default), 16 for senior. The budget is advisory; an explicit `[lint] comment_ratio` overrides it, and `[lint] mode = "strict"` makes it hard, as today. "preset" leaves `RESERVED_SECTIONS`, one definition shared by `config.py` and `doctor.py` (6.9).
- **New keys:**

| Key | Default | Values |
|---|---|---|
| `[preset] level` | `"junior"` | `junior` \| `senior`; any other value reads as junior with a config warning, and the doctor reports D171. `/nh:init` doesn't ask; `nhctl preset senior\|junior [--json]` edits it (6.9) |
| `[turn] max_batch` | `5` | the most steps one approved batch runs (6.3) |
| `[lint.rules]` level `ask` | — | joins `off` \| `hint` \| `error` in `config.load` validation; `LintReport` gains `asks` (6.4) |
| `[guard] shell_install`, `network`, `outside_write`, `harness_toml` | `"ask"` | `ask` \| `warn` \| `off`; one parser covers them and `foreign_mcp` (6.6) |

- **Rule keys:** `package_install` (L009) moves from error to ask; new `network` (L012) and `outside_write` (L013) are ask rules (6.4); `secret_print` (L011) and `secret_name` (L014) are defined in 6.7.

**f. Model-facing text** (D0 e)
- **INSTRUCTIONS** (`app.py`): 2017 → 2017 chars, cap 2048 (the plan estimated 1987; 6.2 has the per-line counts).
  - Rule 1 adds "Only exception: a batch or re-run list the user approved when nh asked."
  - Rule 5 is tightened (defined in 6.2).
  - Rule 7 becomes "RUNNING or QUEUED… ask before re-running".
  - Rule 9 adds "…or print env vars or credentials. Ask before installing packages or writing outside the project."
  - Rule 10 and the pinned lines stay unchanged.
- **Reminder** order: `[NO_PROMPT_ID]` + head + mode part + `drift_line` + `last_cell_line`. The mode parts are an explain part (6.2), an ask-turn part ("ask one question, write nothing") and an approved-batch part (6.3). `RULE` and `QA_REPORT` stay byte-identical.
- **Explanation depth** for the preset goes in the SessionStart context, not the per-turn reminder (6.9).
- **Skills:** new `skills/explain` (6.2), `skills/plan` (6.3) and `skills/review` (6.10). `skills/notebook/SKILL.md` stays ≤ 150 lines; detail moves to a new `reference/asks.md` and the existing `reference/planning.md`. The skill-set pin (`test_skill_files.py:118`) grows to six.
- **Tools:** the count stays 5.

### 6.1 Turn record, classifier, ledger v2 and fail-closed E102

Chunk C1 (plan D0 a, D0 b storage, D1). Files: `_shared/intent.py` (new), `_shared/turn_record.py`, `hooks/nh_hooks/prompt_submit.py`, `policy/turn.py`, `app.py`. Nothing reads `mode`, `request`, `answer` or `pending` yet: C2 (E109), C5 (E122), C6 and C7 (grants) wire them.

**Classifier** (`_shared/intent.py`)
- `classify(text) -> {"mode", "request", "answer"}`: pure, regex only, stdlib, Python 3.9. A non-string gives all None. Matching is case-insensitive on the trimmed text (`’` reads as `'`), with whole words: a word joined to another by `-` or `_` is part of that word, so "fixed" is not "fix", and "make_features", "add-on" and "re-add" are not change verbs.
- **Lead marks (row 2 only).** Before row 2 looks for its first word, it skips everything up to the first letter, digit, `_` or `/`: punctuation, quotes, brackets, markdown markers (`**`, `` ` ``, `>`, `#`, `- `, a code fence) and invisible characters (a BOM, a zero-width space). So `"explain cell 3"`, `` `explain` cell 3 ``, `**explain** cell 3`, `> explain cell 3` and a quoted `"/nh:explain" 3` are explain messages. A digit or `_` is part of the first word, so "1. explain" and "_explain_ cell 3" are not. Missing one of these would fail open (no mode, so no E109); a false hit fails safe (nh writes nothing and the user asks again), as with a message that opens on a column named `explain`. Row 1 and the request rows keep the trimmed text: a quoted `"/nh:plan"` is not the command.
- The first matching row sets `mode` and `request`; `answer` is checked on its own:

| Order | Result | Message |
|---|---|---|
| 1 | mode `plan` | starts with `/nh:plan` (a whole word: `/nh:planning` doesn't count) |
| 2 | mode `explain` | after any lead marks (above), the first word is "explain", or it starts with `/nh:explain`; and no change verb anywhere: fix, change, add, update, rewrite, refactor, make. Base forms only: "fixed", "changes", "adding", "makes" don't count |
| 3 | request `{"batch": true, "n": N}`, mode `ask` | anywhere in the message, not negated: "run next N" or "run the next N"; or "run steps a-b" ("step" too; a hyphen or an en dash, spaces allowed), n = b − a + 1. Needs 2 ≤ n ≤ 999. "re-run the next 3" doesn't count |
| 4 | request `{"rerun_stale": true}`, mode `ask` | anywhere, not negated: "re-run" or "rerun", then optional "all", optional "the", "stale", optional "cell"/"cells" |
| — | `answer` `yes` / `no` | the whole message (40 chars at most), lowercased, commas read as spaces, inner whitespace collapsed and trailing `. ! ; : …` dropped, is in the yes set or the no set. A message ending in `?` is no answer: "yes?" and "ok?" question the offer, they don't accept it |

- **Numbers.** N in "run (the) next N" is 1–3 digits or a number word from "two" to "ten". Ranges take 1–3 digits a side. "run next 1", "run the next one", "run steps 4-4" and "run the next 1000" are nothing, so no hook can write an `n` the gateway can't read back (Python 3.13 refuses integers of over 4300 digits).
- **Negation.** A request is dropped when its "run" or "re-run" follows, in the same clause, one of: not, never, don't, didn't, doesn't, won't, shouldn't, can't, cannot, did, or "no need to", with at most one word between ("did you run", "don't ever run"). So "don't run the next 3", "never run steps 2-4", "why did you run the next 3?" and "don't re-run the stale cells" carry no request; "no, run the next 3" (a comma ends the clause) does. When several phrases match, the first one that isn't negated counts ("run next 1, then run steps 3-5" is steps 3-5). A request only lets a later plain "yes" grant a batch (6.3, 6.5), so dropping one fails safe: the user asks again.
- **Yes set:** yes, y, yep, yeah, yup, sure, ok, okay, go, go ahead, do it, please do, yes please, sounds good, approved.
- **No set:** no, n, nope, nah, no thanks, don't, dont, do not, not now, cancel, stop, skip, skip it.
- "go" is yes only as the whole message: "go on", "go for step 2" and "yes, but drop the nulls" are no answer.
- An explain or plan message carries no request ("/nh:plan run the next 3" is plan only).

**Record fields** (`_shared/turn_record.py`; `RECORD_VERSION` stays 2)

| Field | Set by | Holds |
|---|---|---|
| `mode` | `opened()`, tightened by `absorbed()` | `explain` \| `plan` \| `ask` \| None |
| `request` | `opened()`, `absorbed()` | `{"batch": true, "n": n}` (2 ≤ n ≤ 999) \| `{"rerun_stale": true}` \| None |
| `answer` | `opened()` only | `yes` \| `no` \| None: the opening message's answer |
| `prev_turn_id` | `opened()` | the previous record's `turn_id` (None without one, or after an orphan) |
| `prev_request` | `opened()` | the previous record's `request`, so the next turn sees what a "yes" answers |

- `read()` whitelists the five fields: an unknown mode or answer, a request of another shape (extra keys, n < 2 or > 999, a bool n) or a non-string `prev_turn_id` reads as None; a request is rebuilt with exactly its keys. A v1 record reads them as None.
- `opened(session_id, prompt_id, now, previous=None, classified=None)`: `classified` is `classify()`'s dict (None: all None).
- `aliased()` keeps all five; `orphan()` sets them to None.
- `known(record, prompt_id)`: the record's turn, one of its aliases, a key of `earlier`, or `prev_turn_id` (D1).
- `running(record, prompt_id)`: whether a human message with `prompt_id` was typed into the running turn (below).

**Mid-turn messages** (user decision, 2026-09-29, after V16)
- Claude Code folds a message typed while Claude works into the running turn and fires UserPromptSubmit with the **same** prompt_id as the running turn. The hook detects it with `running(record, prompt_id)` and calls `absorbed(record, prompt_id, classified)` instead of `opened()`. `running()` is true when `prompt_id` is set and is:
  - the record's `turn_id`, for a human record (a turn a human message started); or
  - one of the record's `aliases`: a turn a background task's notification started. A human message never reuses a prompt id otherwise, so an alias id in a human message can only be this case. For a human record it tightens the human turn the alias belongs to; for an orphan, the record stays an orphan (no human turn, so no budget: its writes get the orphan's E102 and the user sends the message again).
- A prompt id in `earlier` is not the running turn (a newer human message has opened since), so a message with one opens a turn as before.
- It can only tighten:

| From the absorbed message | Effect |
|---|---|
| mode `explain`, `plan` or `ask` | becomes the turn's `mode` for the rest of the turn; no mode keeps the turn's |
| request batch or `rerun_stale` | becomes the turn's `request`, so the next reply's `prev_request`; no request keeps the turn's |
| answer yes or no | never an answer: the turn's `answer` stays |
| — | `turn_id`, `prev_turn_id`, `prev_request`, `aliases`, `earlier`, `ts` and `alias_ts` stay; `prompt_id` is the latest seen (the message's: the turn id or the alias); the ledger's `pending` isn't touched |

- It opens no turn, so there is no new cell budget, and the reminder doesn't repeat `RULE`.

**Hook** (`prompt_submit.handle`)
- A notification: unchanged (alias or orphan, §3.7).
- A human message: `classify()` runs inside try/except. On an exception the fields are None, the record is still written, and a `classify_failed` event is logged.
- Mid-turn: `absorbed()`, a `turn_absorbed` event, and no `RULE`. Otherwise `opened()` and `turn_open`.
- Events never hold prompt text:

| Event | Fields |
|---|---|
| `turn_open` | `session_id`, `turn_id`, `prompt_chars`, and new: `mode`, `request`, `answer` |
| `turn_absorbed` (new) | `session_id`, `turn_id` (the running human turn; None for an orphan), `prompt_id` (the message's), `prompt_chars`, and the absorbed message's own `mode`, `request`, `answer` (its answer is logged, never applied). `nhctl metrics` doesn't count it as a turn |
| `classify_failed` (new) | `session_id`, `turn_id` (as its `turn_open` or `turn_absorbed` event) |

- **Reminder:** `[NO_PROMPT_ID]` + head + `drift_line` + `last_cell_line`, clipped at `REMINDER_MAX_CHARS` (400) as before. The head is `human_head(record, absorbed)`: `[RULE]` for a new turn, nothing for an absorbed message; C2 and C6 add their mode parts there, after `RULE`. With nothing to say (an absorbed message into a turn with no mode part, no last cell, no drift) the hook prints nothing. C1 kept v0.1's order of the last two parts (`last_cell_line`, then `drift_line`); C2 settled it as §6.0 f lists it, `drift_line` first (6.2). `test_hook_prompt.py` pins the exact order (a new turn, an absorbed message, and which part the clip cuts), so a change to it is deliberate.

**Ledger v2** (`policy/turn.py`)
- `TurnLedger` saves `{"v": 2, "turns": {...}, "pending": <dict or null>}` and loads v1 (no pending) and v2. Every write, `save()` or a pending helper, keeps only the `KEEP_TURNS` (5) newest turns by `opened_at`.
- `pending` is `{"kind": "cell" | "rerun", "key": <sha256 hex>, "turn_id", "ts"}`: hashes only. On load, any other shape reads as None. `pending_key(text)` is the sha256 hex of the text (UTF-8); 6.4 and 6.5 define what text.
- Helpers (all under `svc.locks.hold` once wired):

| Helper | Does |
|---|---|
| `ledger.pending(session_id)` | the pending question, or None |
| `ledger.set_pending(session_id, kind, key, turn_id, now=None) -> bool` | first ask wins: False (the first is kept) while a pending of the same turn exists; a pending of an earlier turn is replaced |
| `ledger.clear_pending(session_id, turn_id=None)` | drops it (only if it belongs to `turn_id`, when given) |
| `grant(pending, record, turn_id, kind, key) -> (granted, pending_after)` | pure: the table below |
| `ledger.use_grant(session_id, record, turn_id, kind, key) -> bool` | applies `grant()` and saves `pending_after` |

- `grant()`, for a call whose canonical turn is `turn_id`, against the current turn record:

| Situation | Granted | `pending` after |
|---|---|---|
| no pending | no | None |
| the call is in the pending's own turn | no | kept (still waiting) |
| no record, or the call isn't in the record's turn | no | kept |
| `answer == "yes"`, `pending.turn_id == prev_turn_id`, same kind and key | **yes, once** | cleared |
| the same, but another kind or key | no | kept: only the exact call is granted |
| any other answer (no, None), or the pending is older than the previous turn | no | dropped |

- Batch and re-run-stale grants (`answer == "yes"` with `prev_request`) and their `TurnState` fields are C6's and C7's (6.3, 6.5).

**Fail-closed E102** (D1, `TurnGate.on_call_tool`)
- Gate order after `_writer_turn` and E104: E102 for an orphan's turn (as §3.7), then D1, then v0.1's E102 for an older stamp (which also refuses an unknown id older than the record), then E108. C2 inserts E109 before E108 (6.2).
- **D1.** A main-conversation call (no `agent_id`), a turn record of a human message, and a stamp `prompt_id` the record doesn't know (`known()` is False):

| Stamp | Refusal |
|---|---|
| as new as the record's `ts` or newer | E102 with the detail "- nh missed this message; send it again." and Next "Tell the user nh missed their last message and ask them to send it again; write nothing until they do." (v0.1 gave it a fresh budget, so it escaped the message's mode) |
| older than the record's `ts` | not D1: v0.1's plain E102 (the call's id isn't the record's turn) |

- The E102 row of `skills/notebook/reference/errors.md` and `docs/troubleshooting.md` name the missed-message case.
- **Allowed as before:**
  - no record at all: the `/nh:init` message, whose hook runs before `.nh/` exists (`test_skill_files.py` `test_init_skill_expects_no_turn_record_in_its_own_message`);
  - an orphan record (no human turn): an unknown id newer than it keeps v0.1's behaviour, since a background task can finish during the `/nh:init` message;
  - a known id: the turn, an alias, an earlier alias, or `prev_turn_id`;
  - writer calls: their turn comes from the run (`_writer_turn`, E107).
- V16 found no false D1 hit: an absorbed message keeps the turn's prompt_id, and a new message's record is written before its first tool call. V16 typed into a turn a human message started; a turn a notification started is absorbed by the same rule (its prompt id is an alias, which `known()` accepts).

**Tests:** `tests/unit/test_intent.py` (the classifier table, 60+ phrases), `tests/unit/test_turn_record.py`, `tests/unit/test_ledger.py` (v1 load, v2 save, pending, the `grant()` table), `tests/hooks/test_hook_prompt.py` (fields, `classify_failed`, mid-turn absorption; `RULE` and `QA_REPORT` pins unchanged), `tests/gateway/test_gateway.py` (D1), `tests/nhctl/test_metrics.py` (`turn_absorbed` is not a turn). `tests/fakes/turns.py` gains `Turns.prompt(prompt_id, session_id=None, text="…")`.

### 6.2 Explain-only (FR-9)

Chunk C2 (plan D2, D0 c, D0 e). Files: `policy/errors.py`, `app.py`, `_shared/turn_record.py`, `hooks/nh_hooks/pre_tool.py`, `hooks/nh_hooks/prompt_submit.py`, `skills/explain/SKILL.md` (new), `skills/notebook/SKILL.md`, `skills/notebook/reference/errors.md`, `skills/notebook/reference/replies.md`, `docs/troubleshooting.md`, both READMEs, and the eval cases `explain-only` and `slash-explain`. It is the first reader of the record's `mode` (6.1). The mode that 6.1's classifier sets, `explain`, `plan` or `ask`, means the same here for all three. So a `/nh:plan` message (C6's skill) and a "run next 3" message (C6's batch ask) are no-write turns from C2 on.

**`no_write_mode(record, turn_id)`** (`_shared/turn_record.py`)
- Returns the record's mode (`explain`, `plan` or `ask`) when `turn_id` is the record's `turn_id`.
- Else None: no record, no turn, another turn, an orphan's None turn, a v1 record, or no mode.
- The gateway and the workflow guard both pass the call's canonical turn (`canonical()`), so a notification's alias counts as its human turn.

**E109** (`policy/errors.py`)
- First line: "Not {verb}: no notebook change in an explain, plan or ask message." (`{verb}` as for E107/E108: written, run or undone). §6.0 d's "Meaning" becomes this line in the `Not {verb}:` shape of E107 and E108, and says "message" as the other user-facing refusals do.
- Next: `E109_NEXT[mode]`, the turn's mode:

| Mode | Next |
|---|---|
| `explain` | "Answer in chat with a numbered walkthrough; write nothing this message." |
| `plan` | "Reply with the numbered plan; write nothing this message." |
| `ask` | "Ask the user the one question; write nothing until they reply." |

- A writer's E109 (below) takes `RETURN_TO_WORKFLOW` as its Next instead.

**Main conversation** (`TurnGate.on_call_tool`)
- A call is refused with E109 when all of these hold:
  - no `agent_id` (the main conversation);
  - `_is_write(name, args)`: `nh_add_cell`, `nh_edit_cell`, `nh_undo`, or `nh_run` in mode `run` (the default);
  - a turn record exists, and the call's canonical turn is the record's `turn_id`;
  - the record's `mode` is `explain`, `plan` or `ask`.
- Allowed as before:
  - `nh_inspect` (never gated);
  - `nh_run` in mode `wait` or `interrupt`: they only follow a running cell;
  - any call with no turn record (the `/nh:init` message);
  - a turn with no mode, a "yes" or "go" turn included: E109 never outlives its message;
  - a call of an earlier turn that got past E102 (a known id, 6.1): the record's mode belongs to the newer message.
- **Gate order:** E105, E101, E106, `_writer_turn` (E103, E107, the writer's E109), E104, E102 for an orphan, D1's E102, v0.1's E102, **E109**, E108, then the tool.
  - After E102: a missed or older message still gets "send it again" or "wait", which is the stronger answer.
  - Before E108: an explain message typed while nh:qa-cell writes is told to answer in chat, not to wait for a report.
- **Tighten-only.** The gate reads the mode at call time. A mode absorbed mid-turn (6.1) therefore refuses every later write of that turn, including a retry of a cell written before the message arrived. The cell itself stays: E109 only stops changes.

**nh:cell-writer** (`_writer_turn`)
- After the run checks (E103, E107): when the record's mode is `explain`, `plan` or `ask`, the writer's `nh_add_cell` and `nh_edit_cell` get E109 with Next `RETURN_TO_WORKFLOW` (so `_for_writer` adds no `WRITER_LINE`).
- `nh_run` in mode `wait` passes, so the writer can still follow a cell it wrote before the mode arrived. Its other calls were already E103.
- A run of an earlier turn still ends with E107.

**Workflow launch** (`pre_tool.workflow_guard`, advisory)
- Deny order: a subagent, `approve_before_run`, **the mode**, then one run per message.
- The mode check: an nh:qa-cell launch is denied with `WORKFLOW_MODE_REASON` when its prompt's canonical turn is the record's `turn_id` (`canonical(record, prompt_id) == record["turn_id"]`) and the record's mode is `explain`, `plan` or `ask`. The reason reads: "nh: this user message only asks to explain, plan or ask, so no cell is written in it. Don't launch nh:qa-cell: answer in chat (a numbered walkthrough, the numbered plan or the one question) and write nothing."
- Other launches are unchanged. The deny is advisory; the gateway's writer E109 is the enforcement.

**Reminder** (`prompt_submit`)
- **Order, settled here** (§6.0 f): `[NO_PROMPT_ID]` + head + `drift_line` + `last_cell_line`, clipped at 400. The clip now cuts the tail of `last_cell_line` first.
- **Head:** `human_head(record, absorbed)` = `[RULE]` for a new turn (nothing for an absorbed message), then `mode_parts(record)`.
- **Notification head:** `notification()` returns its first line (`QA_REPORT`, `QA_EARLIER` or `BACKGROUND`, each byte-identical), then `mode_parts` of the record it joins (`aliased()` keeps the mode). In an explain turn the writer's add got E109, so `QA_REPORT`'s "unless its writer wrote none" must not read as leave to write.
- **`mode_parts(record)`:** `[EXPLAIN]` when the record's mode is `explain`, else nothing. This is the seam for C6: its ask-turn part (mode `ask`) and approved-batch part (`answer == "yes"` with `prev_request`) go here.
- **`EXPLAIN`:** "[nh] Explain only this message: a numbered walkthrough in chat, never in the notebook; change nothing."
- **Cases:**

| Message | Head |
|---|---|
| a new explain message | `RULE EXPLAIN` |
| a message absorbed into a turn whose mode is now `explain` | `EXPLAIN` |
| a background task's notification in a turn whose mode is `explain` | its first line, then `EXPLAIN` |
| any other message | as in 6.1 |

- `RULE`, `QA_REPORT` and `NO_PROMPT_ID` stay byte-identical.

**Skill** `skills/explain/SKILL.md` (`/nh:explain`)
- Frontmatter: `name: explain`, a description, `argument-hint`, `disable-model-invocation: true`, `allowed-tools: [mcp__plugin_nh_nh__nh_inspect]`.
- Body:
  - find the cell with `nh_inspect(view="outline")`, and read it with `view="cell"`. A bare number is `[n]`, the execution count, not the outline's row number (which counts note and markdown rows too). With no cell named: the cell the user's question is about, if any; else the last cell the reminder names (a title it cut with `…` matches by prefix) or, when the clip cut that line (a long drift line comes first), the last code cell nh wrote (author `agent` or `agent*` in the outline); else, in a notebook nh wrote no cell of, the last code cell that ran;
  - give a numbered walkthrough in chat, one line group at a time, quoting the code and the real values from its outputs;
  - never write the explanation into the notebook, and change nothing (the gateway refuses with E109 when the message names no change; the rule holds either way);
  - end by offering the next step;
  - plain, junior-level depth by default. C9 adds the preset's depth line through SessionStart (6.9).
- The skill doesn't enforce anything. `/nh:explain <text>` reaches UserPromptSubmit as its raw text (spike V1), so the classifier sets mode `explain` and E109 applies even if the skill never loads, as long as the text names no change verb (6.1: fix, change, add, update, rewrite, refactor, make, even as a noun).
- With a change verb ("/nh:explain the add step", "/nh:explain how to make it faster") the message has no mode: no E109, and the reminder carries `RULE`, not `EXPLAIN`. Only the skill's "change nothing" guards it, so the explain skill says to change nothing either way, both READMEs say nh blocks changes unless the message names one, and `skills/notebook/SKILL.md` and `reference/replies.md` let an explain reply change something only when the message names a change verb ("explain and fix …"). Making a leading `/nh:explain` always set mode `explain` would be a classifier change (plan D0 a), not C2's.
- `skills/notebook/SKILL.md`: the explain skill is user-only (`disable-model-invocation`), so on the plain "explain …" path the model has only this skill, the `EXPLAIN` reminder and `reference/replies.md`. The turn section's step 1 points an explain-only message to the **explain** reply ("no cell"), and that reply says the whole contract: `nh_inspect` the cell, a numbered walkthrough in chat quoting its code with the real values from its output, one proposed next step not taken, never in the notebook, and no change unless the message names a change verb (E109). `reference/replies.md`'s explain row says the same and lists the seven verbs: a change asked for in other words ("… and drop them") stays refused, so the reply proposes it as the next step. `errors.md` and `docs/troubleshooting.md` give E109's cause the same way. `skills/notebook/SKILL.md` stays at 150 lines.

**INSTRUCTIONS** (`app.py`): 2017 → 2017 chars (cap 2048, 31 to spare).

| Line | Chars before → after | Change |
|---|---|---|
| Rule 1 | 177 → 249 | adds "Only exception: a batch or re-run list the user approved when nh asked." (§0) |
| Rule 5 | 191 → 173 | "named intermediate results" → "named intermediates"; "end with something visible to check" → "end with a visible check" |
| Rule 7 | 305 → 217 | the same statuses and actions, in the reminder's short form: "RUNNING or QUEUED cell: tell the user, add nothing. Interrupted: ask before re-running or changing it. Deleted by the user: a no. Lost (kernel gone) or not run (the user typed into it first): tell the user and ask." |
| Rule 9 | 132 → 166 | adds ", or print env vars or credentials" after ".ipynb files" (C4's L011 enforces it) |
| Others | unchanged | rule 10 stays byte-identical (`test_qa_workflow.py`) |

- Rules 1, 5 and 9 alone would reach 2105, over the cap. Rule 7 is the other line §6.0 f changes, so C2 tightens it to fit.
- No explain clause: it wouldn't fit without cutting more. The reminder's `EXPLAIN` part and E109 carry the rule instead.

**Docs:** an E109 row in `skills/notebook/reference/errors.md` and in `docs/troubleshooting.md`. `/nh:explain` goes in `plugins/nh/README.md` (Commands) and in the root README's review step.

**Evals** (authored in C2 and committed before they ran; the first run, CLI 2.1.284 with CI's flags, scored both cases 1.0 on the graders as first written; C2's review fix changed the graders below, and their re-run with the same CLI and flags scored both cases 1.0 again, every grader passing in all 6 runs):
- `explain-only`: "explain …" about cell [1] of the shared fixture;
- `slash-explain`: the same ask as "/nh:explain …".
- Both expect a numbered walkthrough in chat, with no `nh_add_cell`, `nh_edit_cell`, `nh_run` or `nh_undo` call, no Read of the `.ipynb`, and no `.ipynb` edit. The last holds by construction: `allowed_tools` is `[Read, Glob, Grep, Skill]` and CI grants nothing, so the run withholds Write, Edit and NotebookEdit and a grader on them could never fail (`never-edits-ipynb` covers the case where they are granted). Nothing in the eval refuses a write either: the suite mocks answer `nh_add_cell`, `nh_edit_cell`, `nh_run` and `nh_undo` with success, and the PreToolUse hook only stamps.
- Graders (a run scores passed weight / total weight, a case the mean of its 3 runs, CI's threshold is 0.8):

| Grader | Type | Weight | Checks |
|---|---|---|---|
| `no-writes` | regex on `mock_calls`, `not_contains`, `arm: both` | 15 | no call of the four write tools: `"tool":\s*"mcp__plugin_nh_nh__(?:nh_add_cell\|nh_edit_cell\|nh_run\|nh_undo)"` (each name whole, for the tool-name lint) |
| `walkthrough` | llm on the last message | 5 | a numbered walkthrough of the cell's parts with its real numbers, nothing written or invented. The judge sees only the last message, so the rubric carries the cell's whole output: without it, a run that also cited the 25 order dates failed all three votes as "invented numbers". `slash-explain` also asks for a next step offered and not taken: there the message is the explain skill's command, and the skill asks for one. On the plain path only the notebook skill asks for it, and a run may answer from the `EXPLAIN` reminder alone, which doesn't, so `explain-only`'s judge would fail a run that followed its text |
| `numbered-steps` | regex on the last message, `flags: i` | 1 | a line that is step 3 of a list: `3.`, `3)`, `3:`, `3 —`, `3 - `, `**3**`, `Step 3`, after optional indent, `- `, `#`s and `**`; not `3.5%`, `3 rows`, `2023.` or `Step 30` |
| `uses-real-numbers` | regex on the last message | 1 | `\b43\b`, the row count |
| `no-read-ipynb` | `tool_used` Read, `input_match: '\.ipynb'`, max 0 | 1 | the skill's "never Read the `.ipynb`" (a hook-denied call still counts) |
| `skill-registered` (`slash-explain` only) | regex on the trace, `arm: with-only` | 1 | the init line's skills list names `nh:explain`: `"skills":\[[^\]]*"nh:explain"`. A `-p` trace never holds the expanded command (checked on a kept 2.1.284 trace), so whether the skill's text loaded can't be graded; a heading grader failed every run. CI runs `--ablation none`, which scores `with-only` graders too |

- Why these weights: with W for `no-writes`, J for the judge and 3 or 4 other weight-1 graders (total T), one writing run in three fails the case when W > 0.6 T (15/23 and 15/24); a judge failing every run fails it when J > 0.2 T (5/23, 5/24); one cheap miss passes (22/23); a reply that does nothing but inspect fails (16/23, 17/24), and one judge fail in three runs passes (0.93). One grader per write tool can't do this: failing a single write then needs each of the four to outweigh everything else, which lifts a do-nothing run over 0.8. `tests/unit/test_skill_files.py` replays these scenarios on the case files.

**Tests:**
- `tests/gateway/test_explain_only.py`: E109's first line and the three Next lines as literals; E109 for add, edit, run and undo in an explain turn, each with the right Next; wait and interrupt allowed (no refusal, their own machine line); plan and ask turns refused; the next plain turn allowed; a mid-turn explain refusing a later retry in the same turn; the mode keyed on the call's canonical turn (an explain turn's notification alias gets E109, an earlier turn's alias its own E110); the writer's E109 with its wait still allowed; add, run, edit and undo with no record allowed; the gate order at both ends of E109: a plan-mode call gets E104 and a writer whose run was already reported gets E107, neither E109.
- `tests/unit/test_turn_record.py`: the `no_write_mode` table.
- `tests/unit/test_intent.py`: 6.1's lead marks (quotes, markdown, brackets, a BOM, a zero-width space) before "explain" or `/nh:explain`, and what they don't cover ("1. explain", "_explain_", a change verb, a quoted `/nh:plan`).
- `tests/gateway/test_gateway.py`: `test_known_ids_pass_d1` absorbs a plain message instead of an explain one, since an explain one now blocks the write it checks.
- `tests/hooks/test_hook_workflow.py`: the mode deny (its reason pinned) in explain, plan and ask turns, for an alias and a mode typed mid-message; the mode checked before one run per message, and after the subagent and `approve_before_run` denies (each keeps its own reason); other launches are allowed.
- `tests/hooks/test_hook_prompt.py`: the new order and clip, the `EXPLAIN` pin, the absorbed-explain head, and a notification's head in an explain turn.
- `tests/unit/test_skill_files.py`: the skill set gains `explain`; the explain skill's frontmatter; both cases' `nh_inspect` mocks match the real gateway's cell view byte for byte (their graders read its numbers), and both cases' own `scaffold.sh` builds the 3-cell fixture with no `last_cell.json`; the grader weights replayed on the scoring scenarios above; `numbered-steps` and `no-writes` against sample answers and call logs; `skill-registered` against sample init lines (and not a reply quoting one); both rubrics carry their mock's whole output; `replies.md`, `errors.md` and `troubleshooting.md` list the classifier's seven change verbs, and the notebook skill's explain reply carries the contract; every §6.0 d code the gateway has gets its `errors.md` and `troubleshooting.md` rows.
- INSTRUCTIONS pins (in `test_explain_only.py`): rules 1, 5, 7 and 9 as changed here, rules 1-10 in order, and at most 2048 chars.

**Perf** (50 interleaved runs, d1cc942 vs C2 after its review, p50/p95 in ms; prompt-submit on an explain message, workflow on a plain message's launch): system 3.9 prompt-submit 117.4/125.7 → 117.5/127.4, workflow 121.2/132.1 → 119.9/129.2; server venv 3.13 prompt-submit 83.0/91.1 → 83.9/90.6, workflow 87.4/97.4 → 86.7/102.8. All under the 150 ms budget. The review fix's lead-mark skip adds 4.7 µs per message to `classify()` on system 3.9 (36.7 → 41.4 µs, five messages up to 4000 chars). Its end-to-end run (50 interleaved runs of prompt-submit, system 3.9) had the machine at load average ~7 from another app holding a core: 8882db8 127.6/161.9, the fix 129.7/154.2. Both are over 150 only from that load, and the fix is no slower.

### 6.8 Redaction of what Claude sees (FR-14) and E125

Chunk C3 (plan D8, and E125 from D7). Files: `_shared/secrets.py` (new), `_shared/text.py`, `_shared/tomlread.py`, `_shared/scaffold/core.py`, `exec/shaping.py`, `exec/docsafe.py`, `render.py`, `tools/common.py`, `tools/write.py`, `tools/undo.py`, `tools/inspect.py`, `policy/errors.py`, `app.py`, `backend/rtc_backend.py`, `hooks/nh_hooks/prompt_submit.py`, `hooks/nh_hooks/session_start.py`, `hooks/nh_hooks/main.py`, `scripts/nhctl/common.py`, `scripts/nhctl/doctor.py`, `scripts/nhctl/freshrun.py`, `scripts/nhctl/lab.py`, `scripts/nhctl/main.py`, `skills/notebook/reference/errors.md`, `skills/notebook/reference/tools.md`, `docs/troubleshooting.md`, `docs/harness-toml.md`, both READMEs, `spikes/v0.2/v11_redaction.py` and `spikes/RESULTS.md`. FR-14 says credentials never reach Claude through nh. C4's lint rules (6.7) stop code that prints them; this section covers what gets printed anyway. The notebook keeps the raw text; only what nh shows Claude, and what nh writes for Claude to read, is redacted.

**The Redactor** (`_shared/secrets.py`: Python 3.9, stdlib only, shared by the gateway, the hooks and nhctl)
- `Redactor.for_project(root)` takes its values from three sources. When two sources hold the same value, the first name wins.

| Source | Redacted when | Marker |
|---|---|---|
| The project's `.env` (`parse_env`) | A secret-shaped name with a value of 8+ chars. Any other name with 16+ chars, unless its last part names a place, an address or a label (`LENGTH_EXEMPT_LAST`: PATH, DIR, DIRECTORY, FILE, FOLDER, NAME, ROOT, BUCKET, CACHE, HOST, URL, USER, DB, SCHEMA, PROJECT, REGION, WAREHOUSE, TITLE, LABEL, CHANNEL and the like) or the value is a file path with an extension (`data/in/sales_2026_q3.csv`: a `/`, a `.ext` ending of 1 to 8 letters and digits, no whitespace, `@` or `://`). Neither exemption applies when a part says the value carries a secret (`SECRET_BEARING_PARTS`: WEBHOOK, HOOK, DSN, SAS, SIGNED, PRESIGNED) or the value is a URL that may carry credentials (`url_may_hold_credentials`: user info, a query, a fragment or a token-like path segment, the rule scaffold uses to send a data URL to `.env` as `DATA_URL`). Also the password of any URL value (`scheme://user:password@`, as written and percent-decoded), 8+ chars, under the variable's name. Never a weak value | `[redacted:NAME]` |
| The process environment | A secret-shaped name with 8+ chars, and the password of a URL value under any name (8+ chars). Never a weak value (PWD and OLDPWD are the shell's directories, never secrets) | `[redacted:NAME]` |
| Jupyter tokens | JUPYTER_TOKEN and NH_JUPYTER_TOKEN in the env (8+), and the token `rtc_backend` discovers (`add_value("JUPYTER_TOKEN", token)`) | `[redacted:JUPYTER_TOKEN]` |

- **Why 16 for other names.** Spike V11's 8-char rule redacted REGION=eu-west-1 (9 chars), which hid a region name in every output. DEBUG=true and DATA_PATH=data/raw.csv stay visible either way.
- **Why the place and label exemption.** A long DATA_DIR, PROJECT_ROOT, S3_BUCKET, TRANSFORMERS_CACHE or MLFLOW_TRACKING_URI would hide the path in every traceback. The exemption never covers what a URL may carry: its password is a value of its own (so a cut inside it is caught too, see Cut pieces, which the `url-userinfo` pattern alone misses), and a URL with user info, a query, a fragment or a token-like path segment (a signed URL's `sig=`, scaffold's `DATA_URL`) is redacted whole. SLACK_WEBHOOK_URL and SENTRY_DSN are still redacted: the value is the secret.
- **Weak values** (`is_weak_value`, `WEAK_VALUES`) are never redacted as values, from any source, whatever the name says:
  - a well-known local default or placeholder word: postgres, changeme, minioadmin, localhost, notebook and the like;
  - the value of its own name, or one of its parts (POSTGRES_PASSWORD=postgres);
  - the user's login name (`$USER`, `$LOGNAME`, `$USERNAME`);
  - a placeholder (`<your-key-here>`, `${X}`, `{pw}`, `%(pw)s`, `***`) or one repeated character (`xxxxxxxxxxxx`). Braces hold a name only: inline JSON (`{"type": "service_account", …}`) is no placeholder;
  - a path to a key file: `/…`, `~/…`, `./…`, `../…` or `C:\…`, ending in .json, .p12, .pem, .key, .keytab or .crt, with no whitespace (GOOGLE_APPLICATION_CREDENTIALS names the file, not the key, and the Google libraries need that exact name; inline JSON stays redacted);
  - `.env` only: a secret-named value, or a URL's password, that the same `.env` also sets under a name nh shows (POSTGRES_USER=POSTGRES_PASSWORD=POSTGRES_DB=superset: the database name would turn into a marker in every output, and E125 would refuse any edit that names it).
  Redacting POSTGRES_PASSWORD=postgres hid every "postgresql" in code and outputs. The patterns still catch a URL's userinfo either way.
- **`parse_env`** is a small stdlib parser. Where python-dotenv (which the user's code uses) reads a line differently, both readings are registered: an extra value only adds an exact match. It handles:
  - `export`, blank lines, `#` comments, a UTF-8 byte-order mark and a single-quoted name (`'API_TOKEN'=…`);
  - single quotes, taken literally up to the next `'` on the line, and also as python-dotenv reads them: up to the first `'` not after a backslash, spanning lines, with `\'` and `\\` unescaped. Parsing goes on at the next line;
  - double quotes with the `\n \r \t \\ \"` escapes, spanning lines. With no closing quote, both the rest of the file and the opening line's own text are registered, and parsing goes on at the next line: python-dotenv skips a statement it can't parse and still loads the later ones;
  - an unquoted value's ` #` comment;
  - `${VAR}` and `${VAR:-default}` (the default only when VAR is unset) in an unquoted or double-quoted value: registered as written, and expanded from the earlier pairs and the environment in both orders (python-dotenv's `dotenv_values` lets the file win, `load_dotenv` the environment). A change to a variable used there rebuilds the Redactor.
  Reading: nh reads `.env` only when it is a regular file: a named pipe (1Password mounts `.env` as one) would block until its writer answers, so it is never opened. A regular file is opened non-blocking, its type checked again on the open file, and at most its first 1 MiB (`DOTENV_MAX_BYTES`) is read, as UTF-8 (a byte-order mark dropped, a bad byte replaced).
  A value is also redacted in the escaped forms an output may show it in: as `cat .env` or `json.dumps` writes it (the `\\ \" \n \r \t` escapes), as Python's `repr` shows it (a string's default display in a notebook, alone or in a container), and HTML-escaped (both `html.escape` forms: `DataFrame.to_html()` printed as text). Each form is added only when it differs, so only a value holding a quote, a backslash, a control character or one of `& < >` costs more. That goes for `.env` and environment values alike.
- **Secret-shaped names** (`is_secret_name`, `SECRET_NAME_PARTS`, `SECRET_PAIRS`; C4's L014 reuses them). A name is split on `_`, `-`, `.` and camelCase into lowercase parts. It matches when:
  - one part is secret, password, passwd, passphrase, passkey, pwd, mysqlpwd, token, credential(s), apikey, accesskey, secretkey or privatekey, or ends in password, passwd or passphrase (PGPASSWORD);
  - two adjacent parts are basic, http or proxy followed by auth (BASIC_AUTH), or api, access, secret, private, auth, signing, encryption, account or master followed by key;
  - the last part is pass or pw (DB_PASS, SMTP_PASS, MYSQL_PW), unless the part before says it's a pass over data (`NOT_PASSWORD_BEFORE`: FIRST_PASS, NUM_PASS);
  - the last part is pat after a code host (`PAT_BEFORE`: GITHUB_PAT, HF_PAT);
  - the last part is key after any part but a lookup-key word (`NOT_SECRET_KEY_BEFORE`: STRIPE_KEY and OPENAI_KEY match; SORT_KEY, PRIMARY_KEY, PARTITION_KEY and RNG_KEY don't). `key` alone doesn't.
  It never matches when there are several parts and the last is file, path, dir, url, uri, name, id(s), count, len, length or size (JUPYTER_TOKEN_FILE, SECRET_NAME, token_ids): those name something about a secret, not the secret. tokens, tokenizer, author, keyboard, PAT, auth and AUTH_MODE don't match.
- **Patterns.** Each is marked `[redacted:<kind>]`, and only the secret part is replaced, so `scheme://`, `@host`, `token=` and `Bearer ` stay visible.
  - **Keyed patterns** (`token`, `password`, `secret`, `key`, `authorization`) read `key=value`, `key = "value"` (spaced only with a quoted value: `token = tokenizer.eos_token` is code), `"key": value`, and `\"key\": \"value\"` inside a JSON string, in any case. A bare value is `[^&\s"'<>]+` (for `key`, also without `;` and `,`), never ending in `, ; ) ] } .` or `\` (the text around it: `f(token=abc)`, `"token=abc."`).
  - **Never a secret**, quoted or not: None, null, nil, true, false, nan, undefined; a marker, or one a view cut (`[redacted…`); a placeholder (`{x}`, `${X}`, `%(x)s`, `%s`); no letter or digit (`***`, `...`); a tokenizer's special token (`[PAD]`, `[CLS]`).
  - **Code, not a secret** (a bare value outside a URL's query): an attribute (`tokenizer.eos_token`), a name of its own kind (`token=hf_token`, `password=db_password`), a call or subscript (`getpass()`, `os.environ[`), and a short name passed on in code: letters and `_` only, one case, under 16 chars, followed by `)`, `,` or `}` (`f(token=hf)`, `{"token": tok}`).

| Kind | Matches | Left alone |
|---|---|---|
| `url-userinfo` | `scheme://user:password@`, redacting `user:password` | A placeholder password: `{password}`, `${DB_PASS}`, `%(pw)s`, `***` |
| `token` | Any key ending in `token` (`token=`, `access_token=`, `HF_TOKEN = "…"`, `"token": "…"`), a quoted value being one word. A credential key (`access_`/`refresh_`/`id_`/`auth_`/`api_`/`bearer`/`session`/`csrf`/`oauth`/`github`/`gh`/`hf`/`slack`/`bot`/`jwt`/`client`/`private`/`secret`/`personal`/`app`/`service`/`security` + `token`) takes any quoted word. A URL query's `?token=`, `&access_token=` (and the other fixed query keys) takes any value; a number only with 6+ digits | A number (`max_token=512`, `"token": 2003`); a quoted word under 12 chars under a plain token key, an NLP token (`{"token": "the"}`); a quoted phrase (`{"token": "New York"}`); `"bos_token": "<s>"` |
| `password` | `password`, `passwd` and `passphrase` keys (`DB_PASSWORD = "…"`, `PGPASSWORD=…`, `"passphrase": "…"`, `?password=`). A quoted value runs to the quote that opened it, on the same line: a passphrase may hold spaces and the other quote (`password="p@ss'word"`) | `password=None`, `password=db_password`, `getpass()` |
| `secret` | `secret` keys (`client_secret=…`, `SECRET = "…"`, `"secret": "…"`, `?client_secret=`), quoted the same way | `secret=None`, `secret=os.environ[` |
| `key` | A key whose whole name is secret-shaped (`api_key=`, `STRIPE_KEY = "…"`, `"apiKey": "…"`, `X-Api-Key: …`, `?api_key=`), with a value of 8+ chars holding a digit within its first 64 (`KEY_DIGIT_WITHIN`), as key material has. A bare value stops at `;` and `,`, so a lookup key's value can't swallow the next pair (`sort_key=abc1;api_key=…`) | A bare `key`; a lookup key (`sort_key`, `partition_key`); a number; `${API_KEY}`; `key=value12345678` |
| `authorization` | `Authorization: token\|Bearer\|Basic …`, and `authorization="…"`, JSON-escaped too | `f"Bearer {token}"` |
| `aws-key` | `AKIA`/`ASIA` followed by 16 capitals or digits, as a whole word | `xAKIA…` |
| `private-key` | `-----BEGIN … PRIVATE KEY-----` through its END line. With no END line within 16 KB (a cut key): the key-shaped characters after it, up to 8 KB | Public keys, certificates |
| `github-token` | `ghp_`, `gho_`, `ghu_`, `ghs_` or `ghr_` followed by 20+ letters and digits; `github_pat_` followed by 20+ | Inside a longer word |
| `api-key` | `sk-` or `sk-ant-…` followed by 20+ chars of `[A-Za-z0-9_-]` that include a digit | `sk-learn-compatible-estimators`, `task-…`, `x-sk-…` |
| `slack-token` | `xoxa-`, `xoxb-`, `xoxp-` or `xoxr-` followed by 10+ | Inside a longer word |

- **Cut pieces.** The kernel cuts long values before nh sees them: reprlib keeps a head and a tail, pandas shortens columns to `…`, and docsafe drops the head of a long stream. So a value of 16+ chars is also redacted in these cut forms, in any case, when it is a secret-named or secret-bearing value (SLACK_WEBHOOK_URL, SENTRY_DSN), a URL that may carry credentials, a URL's password, or a Jupyter token:
  - 12+ chars of its start, right before `...`, `…` or the end of the text;
  - 12+ chars of its end, right after `...`, `…`, a newline or the start of the text.
  A value with a `.` right where it was cut (`Wd8kLq2mZp4x.R7vN` shown as `Wd8kLq2mZp4x...`) runs on into the cut's dots, so the match steps back out of them (for a tail, out of the dots before it).
  This only runs on texts of up to 2 MB. nh's own stream cut (docsafe, below) never leaves such a piece. For a URL nh has no value for, the `url-userinfo` pattern also takes `scheme://user:pass` cut right before `...` or `…` (an all-digit "password" there is a port, and stays).
- **One pass, longest first.** Every value and cut piece is found with `str.find` on one lowercased copy of the text with the same offsets (`_fold`, which the keyed patterns scan too), so `value.upper()` and `value.lower()` are caught at no extra cost; values that differ only in case count as one, and the first name wins. Every value, cut piece and pattern is located on the original text's offsets. Overlapping spans merge into one marker, named after the heaviest span in the group: the longest value (a pattern weighs nothing), and among equals the first to start, then the longest. A value holding `[` or `]` is looked for again, up to three times, since a marker next to it can complete it (`[redacted:A]]x`). The output never contains an input value. It is idempotent: a marker is never matched again. Nothing else is shortened or dropped.
- **`redact_head(text, limit)`**, for callers that then cut at `limit`, redacts a window of `text` and doesn't scan the rest. The window starts at `limit + margin` chars, where margin = max(1024, the longest value + 1), and doubles until its redacted form still reaches `limit + margin` (markers are shorter than what they replace) or it holds the whole text. So a secret that crosses the cut is replaced whole, and the window's own raw edge stays past the caller's cut.
- **docsafe's cuts start at a boundary** (`docsafe.snap_cut`). When docsafe drops the head of a stream too long for the notebook, or of a traceback line over `BUNDLE_MAX_CHARS`, the cut moves forward to the next newline, else whitespace, else one of `,;"'&<>()[]{}`, looking at most min(4096, `STREAM_KEEP_CHARS`/2) chars past it. A cut mid-token would keep a tail no pattern matches (`…E3r4T5` of a GitHub token, the end of a plain-named value). The window depends only on the kept text, so each flush still only appends.
- **Caching and install**
  - `for_project` caches one Redactor per root. It is rebuilt when the `.env` changes (mtime_ns, size, ctime_ns, inode: `utime` can't set ctime, so an edit that restores the size and mtime is still seen, and so is a chmod), when the qualifying env values, a variable a `${VAR}` uses, or the login name change, or when a value is added. A `.env` that couldn't be read (a chmod 000, a race) is not cached, so the next call tries again; one that isn't a regular file is never opened, and its values rely on the environment and the patterns.
  - `install(r)` and `current()`:
    - the gateway installs `for_project(project)` in `_Runtime.services()`, on the first build and again on every later call (a cache hit costs one `stat`);
    - nhctl installs one for its project in each command;
    - the hooks build one for the event's project.
  - Before anything is installed, `current()` is patterns-only, never the identity.
  - `add_value` rebuilds the installed Redactor.

**Wiring.** Each site redacts before it cuts.

| Site | What is redacted |
|---|---|
| `shaping.redact`, which delegates to `secrets.current().redact`, called by `_clean` | Every stream; every text/plain, markdown, LaTeX and JSON body; each error's name, value and traceback frames. This covers the shaped text and the full copy under `.nh/outputs`, which is joined from the already-redacted sections: `_write_once` makes no second pass, and `_save_originals` writes image bytes untouched |
| `shaping._html_to_text` | HTML markup over 1 MB: `redact_head` runs before the 1 MB cut |
| `shaping.summarize_outputs` | The error summary, and `text_head`: `_first_line` starts its window at the first non-blank character, cleans it as `_clean` does (terminal codes stripped, then redacted) and takes the first line only from the part that stays a margin clear of the window's raw edge, doubling the window until that line ends inside it (or runs past the 80-char cut, or the window reaches 1 MiB). So a secret across the window's edge goes whole |
| `docsafe.output_summary` | The error (160 chars) and `text_head` (200 chars): terminal codes are stripped first, as `shaping._clean` does (`_shared/text.terminal_text`: ANSI codes, `\r` overwrites, control characters), so a value split by a color code is still found; then a window growing like `redact_head`'s is redacted, then cut. The outline, the turn summaries and `nh_run(mode="wait")` read these ("Nothing is running: … failed with …" is built from this summary, and `text_result` redacts it again) |
| `docsafe._cap_one` | Nothing itself: its cuts start at a boundary (`snap_cut`, above) |
| `render.error_summary`, `render.cell_label`, `render._short`, `render._describe`, `render._diff` | The text before each cut: error summaries (`redact_head`), the label's title and first code line, and the scalar reprs; then each name summary and change line whole (`@_redacted`), so the var view and run reports |
| `write._error_lines` | The failing code line. The error summary goes through `render` |
| `write.diff_lines`, `write.user_change`, the note head (E141), and `undo`'s E141 | The whole old and new source (or note) before the 6-line cut, so a private key whose END line falls past the cut is still one marker. When only a hidden value changed, the redacted diff is empty and E141 says "(only a hidden [redacted:…] value changed)". Every E141 check and sha runs on the raw text |
| `inspect._clip`, `_first_line`, `_status_lines` | Every view's whole text before its clip: the cell view (source, notes, outputs), the var view (head, text), outline rows, and status (env.json values, backend URLs) |
| `tools.common.text_result` | Every tool result's text: the last safety net |
| `policy.errors.scrub`, which delegates to `current().redact` | Every NhError message (including E141's diffs), the event log (`log.py`), E199's message (before its 300-char cut), the gateway's logging filter (each message, its traceback and its `stack_info`), and the backends' error texts |
| `app._Runtime.services` | Nothing itself: it installs the project's Redactor, from the first call on |
| hooks: `prompt_submit` | The last cell's label (its title before the cut), then the whole reminder before its 400-char clip (the notebook's name, the status, the drift names) |
| hooks: `session_start` | The goal before its 200-char cut, then the whole context |
| hooks: `main.log_failure` | The traceback written to `hooks.log` |
| nhctl: `common.scrub`, which delegates to the installed Redactor (`main` installs the environment's, `project_root` the project's; `doctor` calls `project_root` too) | D151's log tail, `common.tail` (lab's D145/D146 and `lab status --log`, and envsync's `log_tail`, which it also writes to `.nh/state/env-sync.json`), lab and envsync echoes, every printed error (D199) and its `NHCTL_DEBUG=1` traceback on stderr, usage errors (D100, which echo their arguments), and `print_result`: everything nhctl prints, text or `--json` (each string in the data before `json.dumps` escapes it, `scrub_data`, then the whole line), as the last safety net. `tomlread`'s fallback parser names a bad value's key and line, never the value |
| nhctl: `freshrun` | `failing_report`: the failing cell's name and evalue (the runner, which runs in the project env without nh's code, hands back 8,000 raw chars; nhctl redacts with `redact_head`, then cuts to 500); `cell_label`: the title, or the first code line before its 39-char cut; and the reason line |
| `rtc_backend._server` | Nothing itself: it passes the discovered token to `add_value`, so the token is redacted everywhere |

- Code shown to Claude is redacted: the cell view, labels and the failing code line. `nh_edit_cell`'s `base_sha` and every comparison run on the raw source.
- Review files (plan D8's "nhctl … review files") arrive with `/nh:review` in C10, which writes them through `common.scrub`.

**Kept raw.** These are either not shown to Claude by nh or have to stay exact:
- RTC writes and the `.ipynb`: the user's notebook holds what the kernel printed.
- The history store and undo: undo restores the exact cell.
- Every sha and comparison: `base_sha`, `review_earlier_cells`, pending keys (sha256, never text), the writer checks, and E141's check for a user change.
- Image base64 and bytes (`_take_image`, `_save_originals`): never scanned (V11).
- The ledger: it holds only turn state and sha256 keys, never output text.
- `.nh/logs/jupyterlab.log`: the JupyterLab server's output, and what its kernels write to their file descriptors (subprocess, `os.system`, C libraries: ipykernel echoes that to the server's stdout). The server is detached and outlives nhctl, so nothing can relay it; `nhctl lab start` scrubs the old content before it starts a server. nh points Claude at `nhctl lab status --log` (a scrubbed tail), never at the file.
- `.nh/logs/env-sync.log`: uv's or conda's output as they wrote it. What nhctl prints from it and the `log_tail` it keeps in `.nh/state/env-sync.json` pass `common.tail`'s scrub.

**E125** (`policy/errors.py`, `tools/write.py`)
- First line: "Not written: the code holds nh's [redacted:…] marker, not the real value."
- Next: "Read the value from the environment or the project's .env without printing it, or ask the user to edit that line in JupyterLab." nh doesn't load `.env` into the kernel, so `errors.md` says to fall back to `.env` as the first cell does, and that a `[redacted:<kind>]` marker names no variable (ask the user).
- `nh_add_cell` and `nh_edit_cell` (the main conversation's and the writer's) check `code` for `[redacted:` along with the other argument checks. The check comes after the turn checks and before the notebook is resolved, before lint, and before any lock or write. Title, notes and intent aren't checked: they are prose and never run.
- A writer's E125 gets `WRITER_LINE`. It doesn't count as a lint reject (E121).
- The raw value is never restored on edit (plan default 8). Claude never sees the value, so it can't write it back.
- E125 is in `CATALOGUE`, so C2's doc-row test requires its `errors.md` and `troubleshooting.md` rows.

**Performance**
- **Rule:** one pass per text.
  - Values are found with `str.find`.
  - Every pattern starts with a literal, so the regex engine skips ahead at C speed (a leading lookbehind made a regex about 40 times slower, and one alternation of anchors about 13 times slower than separate scans).
  - The case-sensitive ones (`aws-key`, `github-token`, `api-key`, `slack-token`, PEM) are scanned with `search`, the word-boundary check done in Python. `search` finds only the literal and the minimum length (`sk-` and 20 chars); a hit is extended to its full length only after its start is accepted, and the scan then goes on from its end. A long run such as `task-0-task-1-…` or `sk-sk-sk-…` therefore stays linear: a greedy match that is then turned down would rescan the rest of the run from every literal in it.
  - The keyed ones run with `finditer` on one lowercased copy of the text with the same offsets (`_fold`: İ, the only character whose lowercase is longer, becomes "i" first). Every skip rule is inside the regex, so a benign hit (`max_token=512`, `{"token": "the"}`, `token=tokenizer.eos_token`) never reaches Python. Python 3.9's `re` has no atomic groups or possessive quantifiers, so the code rules use a lookahead's capture matched again by reference, `(?=(?P<x>…))(?P=x)`, which is never backtracked into. Only the case-dependent checks (`_code_word`, `key`'s name) run in Python, on the hits.
  - The userinfo regexes (the `@` form and the cut form) are bounded (256 chars of user, 512 of password), so they never run away on a long line. The cut form runs only when the text holds `...` or `…`, which it needs: scanning every `://` for it cost ~80 ms per 50 MB of URLs. The same goes for `key`'s digit check, which looks at most 64 chars ahead: an unbounded one rescanned a long digit-free run (`a_key=a_key=…`, a `;`-joined config) at every `key` in it (1 MB of `a_key=` takes 0.33 s on Python 3.9 with 64, 0.93 s with 256).
  - The keyed regexes are compiled the first time a text holds their literal (`token`, `pass`, `secret`, `key`, `authorization`), not at import: a hook runs in a fresh process, and compiling all five took ~8 ms on Python 3.9, which pushed session-start's p95 to 150 ms.
  - Image base64 and bytes are never scanned, the full copy gets no second pass, and head-only sites use `redact_head`.
- **Budget:** turn overhead p95 ≤ 1.5 s, for a 50 MB stream and a 20 MB image bundle.
- **V11 re-measured** (`spikes/v0.2/v11_redaction.py`, p50/p95 in ms, `shape_outputs` end to end on Python 3.13.8, off (an identity redactor) vs patterns only vs a ~30-value `.env`). The 50 MB profiles each repeat one line: `key=value` logs full of benign token keys (token-kv), JSON records with a short `"token"` value (json-records), NLP token dumps (nlp-tokens), e-mails and URLs, a real token on every line (token-dense, 1.35 million secrets), and an `.env` value on every line (value-dense):

| Case | Off | Patterns only | 30-value `.env` | Added at p95 |
|---|---|---|---|---|
| 5 MB bundle (5 MB stream, 5 MB PNG, two 2 KB outputs), 20 runs | 112.2/121.8 | 144.4/156.6 | 169.4/172.4 | 51 |
| 50 MB stream, 10 runs | 355.8/360.7 | 705.0/725.3 | 945.7/975.5 | 615 |
| 20 MB image bundle (four 5 MB PNGs, a short stream), 10 runs | 167.7/170.6 | 170.8/198.7 | 171.5/203.4 | 33 |
| 50 MB token-kv, 5 runs | 353.9/386.7 | 1219.2/1238.1 | 1474.0/1486.5 | 1100 |
| 50 MB json-records, 5 runs | 360.6/367.5 | 794.2/829.8 | 1040.5/1273.2 | 906 |
| 50 MB nlp-tokens, 5 runs | 371.5/382.2 | 779.4/806.2 | 1012.2/1025.3 | 643 |
| 50 MB emails+urls, 5 runs | 355.9/362.0 | 703.0/718.3 | 969.1/1053.5 | 692 |
| 50 MB value-dense, 5 runs | 353.3/365.9 | 642.8/658.0 | 1253.0/1282.1 | 916 |
| 50 MB token-dense, 5 runs | 354.7/430.9 | 2208.7/2509.9 | 2388.8/2418.1 | 1987 (over) |

- Every planted secret (one per pattern and three `.env` values) is gone from the shaped text and the full copy.
- `Redactor.redact` alone (`--redactor-only`), with the `.env`, p50/p95 on Python 3.9.6 (hooks and nhctl, which redact short heads and tails) and 3.13.8 (the gateway): 50 MB stream 659/699 and 585/603 ms; 5 MB bundle 66/69 and 59/62 ms; token-kv 1310/1492 and 1115/1121 ms; value-dense 985/987 and 911/926 ms; token-dense 2630/2780 and 2031/2098 ms.
- The review of C3 (values matched in any case, their escaped, `repr` and HTML forms, the cut userinfo form, pieces of secret-named values, terminal text) costs, against the C3 commit measured the same day, `.env` p95: 50 MB stream 927 → 976 ms, token-kv 1402 → 1487 ms, emails+urls 883 → 1054 ms, value-dense 1223 → 1282 ms, 5 MB bundle 170 → 172 ms. Most of it is the values' search in the folded text; patterns only on the redactor alone, 50 MB stream 296 → 328 ms p50.
- The budget holds with no cache or trim for every profile but token-dense: the worst adds 1.10 s at p95 (token-kv), where the C0 prototype's 50 MB planted stream took 1.58 s. token-dense costs about 1.3 µs per secret found (each hit is a Python-level span and a marker), so an output with a real secret on every line goes over (a known gap).
- value-dense uses the longest `.env` value: under a shorter value, whose marker is longer than the value, the redacted full copy grows past `prune_outputs_dir`'s 50 MiB cap and is pruned at once (the spike then finds no full copy).

**Known gaps**
- Encoded secrets: base64 and URL-encoded (`%40`, except a URL password, which is also registered decoded). JSON-escaped only in part: a `.env` or environment value's escaped, `repr` and HTML forms and `\"key\": \"value\"` are caught, `\u…` escapes are not; a patterns-only secret's `repr` or HTML form is caught only where the pattern still reads it.
- A secret split across lines, or across outputs (one stream message ends mid-secret), other than the cut shapes above.
- Secrets under the thresholds: a 7-char password, a 15-char value under a plain name, a cut piece under 12 chars.
- Patterns-only secrets (not in `.env` or the env) that look benign:
  - a quoted word under 12 chars under a plain token key, taken for an NLP token in any case (`{"token": "abc12345"}`, `token=b"abc123XYZ"`); a credential key (`"api_token": "abc12345"`) is still caught;
  - a number of under 6 digits in a URL's query (`?token=12345`);
  - a key none of the patterns names (`"auth": "…"`, `pwd=…`).
- A weak value is never redacted: a real password that is also a well-known default (`postgres`), the user's login name, a key-file path, or a value the same `.env` shows under a plain name (POSTGRES_DB) stays visible (a URL's userinfo is still caught).
- A non-secret value is redacted anyway when its name is secret-shaped (8+ chars), or when it has 16+ chars and its name's last part isn't a place or label word and it isn't a file path (`MODEL_VARIANT=gradient-boosted-v7`): the agent can't read it in an output, and E125 refuses an edit that keeps a line holding it. E125's docs say to rename it so the last part says what it holds (MODEL_VARIANT → MODEL_VARIANT_NAME) or to unset it; a name a library fixes can't be renamed.
- Cut pieces of other plain-named values (`license-prod-00...`): only secret-named and secret-bearing values, credential URLs, URL passwords and the Jupyter token are matched in pieces.
- Patterns-only secrets: a quoted token or key holding a quote or a space; `password: value` with an unquoted key (YAML); a key's value whose first digit comes past its 64th char; the tail of a URL password cut before `…` (the head is caught).
- A `.env` that isn't a regular file (1Password's mounted named pipe) is not read, nor more than the first 1 MiB of one: those values rely on the environment and the patterns.
- Code that holds the text `[redacted:` on purpose is refused too (E125); `errors.md` says to build it in pieces (`"[" + "redacted:"`).
- `.nh/logs/jupyterlab.log` holds kernels' fd-level output raw until the next `nhctl lab start` (see Kept raw).
- Secrets drawn in images or held in binary outputs.
- A value that is itself part of a marker ("redacted", or the secret's own name).
- Cost: an output with a real secret on nearly every line. A 50 MB stream of 1.35 million tokens adds ~2.0 s at p95, over the 1.5 s budget (see Performance). Benign-dense outputs (token keys, NLP tokens, e-mails, `.env` values) stay under it.
- A full copy over 50 MiB is pruned as soon as it is written (v0.1's `prune_outputs_dir` cap). Markers longer than their values can push a copy that was just under the cap over it.

**Docs:** E125 rows in `skills/notebook/reference/errors.md` and `docs/troubleshooting.md` (for a `[redacted:NAME]`, read NAME from the environment or `.env` without printing it; a `[redacted:<kind>]` names no variable, so ask the user; never retype a marker; why a non-secret was hidden and how to rename it). `harness-toml.md` and `reference/tools.md` say the `.nh/outputs` copy is redacted like the result while the notebook stays raw, and both READMEs say what nh hides from the agent and the main gaps. The troubleshooting log table points at `nhctl lab status --log`. The re-measure goes in `spikes/RESULTS.md` under V11.

**Tests:**
- `tests/unit/test_secrets.py`:
  - each source and threshold (REGION=eu-west-1, DEBUG=true and DATA_PATH=data/raw.csv are kept), the exemptions and the `.env` parser;
  - name matching, including the must-not-match list;
  - each pattern and its false positives (code names, placeholders, sk-learn), and that a DataFrame repr's columns are unchanged;
  - cut pieces, overlap and longest first, the marker format and idempotence;
  - `redact_head`, caching, `install`/`current`/`add_value`;
  - a Python 3.9 import, and that a keyed regex is compiled only once a text holds its word;
  - the review's cases: each pattern's hits and misses (ML token keys, NLP tokens, code names, lookup keys, the İ fold), weak values, the place and label exemption, equal-weight ties, a value a marker completes, and `redact_head`'s growing window;
  - the C3 check's cases: the parser against python-dotenv (a BOM, a quoted name, both single-quote readings, an unclosed quote, `${VAR}` both ways), a FIFO `.env` and a symlink to one (under a timeout), a chmod 000 then 600 `.env` and a same-size edit with its mtime restored, credential URLs and URL passwords (whole, cut by reprlib, patterns-only cut, a port kept), secret-bearing cut pieces, the repr and HTML forms, upper- and lower-case values, the length and path exemptions, a shared value, a key-file path, a quoted phrase holding the other quote, `sort_key=…;api_key=…`, and 1 MB timing guards (under 2 s) for the case patterns and `key`.
- One test per wiring site, each written so that the site's own call is the only one that could pass it (a later net would otherwise hide a missing or misordered call), in `tests/gateway/test_secrets_wiring.py` (the gateway's sites; `_clip`, `_error_lines` and the log filter's `stack_info` directly; `services()` from the first call; `text_result`; `rtc_backend`'s token) plus the site's existing test file:
  - `test_shaping.py` (replacing v0.1's identity pin; a spy shows image base64 is never scanned), `test_docsafe.py` (a stream cut never starts inside a secret, `snap_cut`, append-only flushes, a long traceback line), `test_render.py` (an error summary cut inside a secret; `_describe` and `_diff`), `test_r2_render.py`;
  - the hooks' `test_hook_prompt.py` (the title before its cut, and the whole reminder) and `test_hook_session.py`, plus the hook failure log in `test_hook_shim.py`;
  - nhctl's `test_freshrun.py` (labels and `failing_report` called directly, on both Pythons), `test_cli.py` (printed and usage errors, the `NHCTL_DEBUG=1` traceback, `--json` strings before escaping, `scrub_data`) and `test_settings.py` (a printed diff);
  - `test_log.py` and `test_lab.py`, whose `***` marks become `[redacted:token]`.
- E125: add and edit, from the main conversation and the writer (with `WRITER_LINE`); nothing written; no lint reject counted. Its place in the gate order: marked code that also fails lint gets E125 and counts no reject, and marked code for a missing notebook gets E125, not E132.
- E141 (`test_secrets_wiring.py`): a private key whose END line is past line 6 is one marker in edit's diff, in "The cell now reads" and in undo's diff, and a change only inside a hidden value says so, for edit and undo. `test_shaping.py`'s `_first_line` cases pad the head so under 12 chars of a secret fall inside the first window; `test_docsafe.py` splits a value with an ANSI code; nhctl's tests show each nhctl site on its own (`scrub_data` for a value with a control character, the line net with `scrub_data` patched out, `tail`, envsync's `env-sync.json`, D199's message before `print_result`, `lab status --log`, doctor's D131 on both Pythons).
- Raw vs redacted: with the fake backend, the notebook, the history and a cell view's `base_sha` hold the raw value while the tool result and the `.nh/outputs` copy hold the marker, and the ledger holds no raw text. An integration test (`tests/integration/test_redaction.py`, marked `integration`) shows the same against a real JupyterLab: the `.ipynb` and the RTC document are raw, the tool result is redacted, and a cell that prints the discovered Jupyter token (built from two halves) shows `[redacted:JUPYTER_TOKEN]`.

**Perf** (hooks, 3 warm-ups and 50 interleaved runs, C2 vs C3, p50/p95 in ms): through the shim, on a project with a 30-value `.env`, a goal and a last cell: system 3.9 prompt-submit 112.1/119.9 → 114.0/121.1, session-start 133.0/140.6 → 134.3/144.8; server venv 3.13 prompt-submit 79.9/84.4 → 81.0/87.0, session-start 99.0/108.8 → 101.5/110.2. A second run: p50 up 1.8 to 3.3 ms, every C3 p95 at most 141.0 ms. All under the 150 ms budget. Before the keyed regexes were compiled lazily, system 3.9 session-start reached 150.9 ms at p95.
