"""The nh MCP server: five tools behind a fail-closed turn gate (plan §4)."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations
from pydantic import Field

from ._meta_rules import tool_meta
from ._shared import turn_record
from ._shared.paths import Layout, find_project
from .backend.base import NotebookBackend
from .config import ConfigCache
from .history import HistoryStore
from .log import EventLog
from .policy import stamps
from .policy.errors import E109_NEXT, RETURN_TO_WORKFLOW, WRITER_LINE, NhError, scrub
from .policy.turn import CURRENT_TURN, NotebookLocks, TurnContext, TurnLedger
from .state import DriftStore, StaleStore
from .tools import inspect as inspect_tool
from .tools import run as run_tool
from .tools import undo as undo_tool
from .tools import write as write_tool
from .tools.common import CALL_LEAD, Services

log = logging.getLogger("nh_gateway")

CALL_DEADLINE_S = 105.0  # stay under Claude Code's 120 s auto-background
# D1's E102 (design §6.1): the user must resend, so the model must tell them.
MISSED_DETAIL = "- nh missed this message; send it again."
MISSED_NEXT = (
    "Tell the user nh missed their last message and ask them to send it again; write "
    "nothing until they do."
)

INSTRUCTIONS = """\
Notebook Harness (nh): you pair with a data scientist in their live Jupyter notebook. They watch cells appear in JupyterLab and must understand each one. The tools enforce these rules; when a tool refuses, follow its "Next:" line.
1. One code cell per user message: nh_add_cell (new) or nh_edit_cell (existing). If its run fails, fix that same cell with nh_edit_cell (at most 2 times), then stop and explain. Only exception: a batch or re-run list the user approved when nh asked.
2. Broad asks ("build a churn model"): write no code. Reply with 5-12 numbered one-cell steps and ask which to start.
3. Call nh_inspect before your first write in a session and whenever you need names, columns or dtypes you have not seen in a tool result.
4. The only markdown is the note nh_add_cell builds from `title` (<=8 words: what the cell does) and `notes` (2-5 plain bullets: what and why). Never display Markdown/HTML or print prose from code.
5. Readable, simple code: one idea per line, named intermediates, plain pandas, UPPER_CASE constants for judgment calls, no functions until reused, end with a visible check.
6. After every cell, reply with: what it did, why, judgment calls, the real numbers (surprises first), failed attempts, and one next step as a title the user can approve with "go". Name cells by title and [n]; never mention nh- ids or line numbers.
7. RUNNING or QUEUED cell: tell the user, add nothing. Interrupted: ask before re-running or changing it. Deleted by the user: a no. Lost (kernel gone) or not run (the user typed into it first): tell the user and ask.
8. Undo: call nh_undo, then say which variables still hold undone results.
9. Never Read, Write, Edit, NotebookEdit or Bash-modify .ipynb files, or print env vars or credentials. Ask before installing packages or writing outside the project.
10. When a reminder says ultracode is on, or on /nh:qa-cell, launch the nh:qa-cell workflow instead of writing; write nothing until its report arrives.
Load the skill nh:notebook for the full rules."""

DESCRIPTIONS = {
    "nh_inspect": (
        "Read-only view of the live notebook and kernel. view: status (connection, kernel, hooks), overview "
        "(outline + variables, default), outline (cells with ids, titles, run status, STALE and edited flags), "
        "vars (variables with shapes, dtypes, nulls), var (one variable; rows <= 20 of its head), cell (one "
        "cell's source, intent and outputs; returns its sha for nh_edit_cell), intents (why each nh cell "
        "exists). Call it before your first write in a session. Never Read .ipynb files."
    ),
    "nh_add_cell": (
        "Insert ONE code cell with a note above it (### title <= 8 words + 2-5 bullets: what the cell does "
        "and why) and run it now in the user's kernel. Once per user message. Pass title and notes as plain "
        "text without '#' or '-'. intent = the user's ask in one line. code: plain, readable Python; no '# %%' "
        "separators, no package installs, no markdown output. Returns the real output, a before/after "
        "self-check and readability hints."
    ),
    "nh_edit_cell": (
        "Replace the code of ONE existing cell and run it. Use it when the user asks to change a specific "
        "cell (this is the message's one cell), or to fix the cell you wrote this message after it failed "
        "(at most 2 retries). Pass base_sha from nh_inspect(view='cell') when editing a cell you did not just "
        "write. title/notes update the note above nh's own cells; human cells never get a note."
    ),
    "nh_run": (
        "mode='run' re-runs one existing cell unchanged; it counts as the message's one action, so only do "
        "it when the user asks. mode='wait' waits up to about 100 s more for a cell reported as RUNNING. "
        "mode='interrupt' stops a running nh cell (only after it has started)."
    ),
    "nh_undo": (
        "Undo nh's last change: removes a cell nh added (with its note) or restores the code from before "
        "nh's edit. Kernel variables are NOT rolled back; the result says which ones still hold undone "
        "results. Later cells that used them are marked outdated. Refuses if the user edited the cell since; "
        "pass force=true only after they confirm."
    ),
}


Notebook = Annotated[
    str | None,
    Field(description="Notebook path relative to the project. Default: the active notebook."),
]


class _TokenScrub(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = scrub(record.getMessage())
        record.args = None
        return True


class _Runtime:
    """Builds services lazily so /nh:init can create .nh/ after the server started."""

    def __init__(self, project: Path | None, backend: NotebookBackend | None) -> None:
        self._project = project
        self._backend = backend
        self._services: Services | None = None
        self.cc_pid = (
            int(os.environ["NH_CC_PID"])
            if os.environ.get("NH_CC_PID", "").isdigit()
            else os.getppid()
        )

    def services(self) -> Services | None:
        if self._services is not None:
            return self._services
        project = self._project or find_project()
        if project is None:
            return None
        layout = Layout(project)
        cache = ConfigCache(project)
        backend = self._backend
        if backend is None:
            from .backend.rtc import RtcBackend

            backend = RtcBackend(layout, cache)
        self._services = Services(
            project=project,
            layout=layout,
            config_cache=cache,
            backend=backend,
            ledger=TurnLedger(layout),
            locks=NotebookLocks(layout),
            history=HistoryStore(layout),
            stale=StaleStore(layout),
            drift=DriftStore(layout),
            events=EventLog(layout),
        )
        _attach_file_log(layout)
        stamps.gc(layout, float(cache.current()["turn"]["stamp_ttl_s"]))
        stamps.gc_runs(layout)
        return self._services

    def require(self) -> Services:
        svc = self.services()
        if svc is None:
            raise NhError("E105")
        return svc


class TurnGate(Middleware):
    """Every write tool must present the stamp its PreToolUse hook wrote (plan §4.3). Fails closed.

    A subagent's stamp passes only for nh:cell-writer in an open nh:qa-cell run of the current
    human message; while such a run is open, the main conversation's writes wait (E108). An
    explain, plan or ask message changes nothing (E109).
    """

    def __init__(self, runtime: _Runtime) -> None:
        self.runtime = runtime

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: Callable[..., Awaitable[Any]]
    ) -> Any:
        name = context.message.name
        if name == "nh_inspect":
            return await call_next(context)
        svc = self.runtime.require()
        cfg = svc.config()
        stamp = stamps.claim(
            svc.layout,
            name,
            context.message.arguments or {},
            my_cc_pid=self.runtime.cc_pid,
            ttl=float(cfg["turn"]["stamp_ttl_s"]),
        )
        if stamp is None:
            raise NhError("E101")
        if isinstance(stamp, stamps.Ambiguous):
            raise NhError(
                "E101",
                detail="- Two identical calls arrived together (one from a subagent), so nh "
                "can't tell whose this one is.",
                next_step="Retry this call once.",
            )
        if not stamp.prompt_id:
            raise NhError("E106")
        args = context.message.arguments or {}
        # A background task's prompt is an alias of the human message it finished in.
        record = turn_record.read(svc.layout, stamp.session_id)
        turn_id = turn_record.canonical(record, stamp.prompt_id)
        run_id: str | None = None
        if stamp.agent_id:
            turn_id, run_id = await self._writer_turn(svc, name, args, stamp, record)
        if stamp.permission_mode == "plan":
            raise NhError("E104", next_step=RETURN_TO_WORKFLOW if run_id else None)
        if turn_id is None:
            raise NhError("E102", detail="- A background task finished; no user message is open.")
        if (
            not stamp.agent_id
            and record
            and record["human"]
            and not turn_record.known(record, stamp.prompt_id)
            and stamp.ts >= record["ts"]
        ):
            # D1 (design §6.1): a prompt id the hook never recorded, newer than the record, is
            # a message nh missed, whose mode would otherwise escape it. An older unknown id
            # gets v0.1's E102 below.
            raise NhError("E102", detail=MISSED_DETAIL, next_step=MISSED_NEXT)
        if record and record["ts"] > stamp.ts and turn_id != record["turn_id"]:
            raise NhError("E102")
        # E109 (design §6.2): an explain, plan or ask message changes nothing. After E102, so a
        # missed or older message still hears "send it again" or "wait"; before E108.
        mode = turn_record.no_write_mode(record, turn_id)
        if not stamp.agent_id and mode and _is_write(name, args):
            raise NhError("E109", next_step=E109_NEXT[mode], verb=_VERBS.get(name, "written"))
        if (
            not stamp.agent_id
            and _is_write(name, args)
            and turn_record.open_runs(svc.layout, stamp.session_id, turn_id)
        ):
            raise NhError("E108", verb=_VERBS.get(name, "written"))
        token = CURRENT_TURN.set(
            TurnContext(
                session_id=stamp.session_id,
                prompt_id=turn_id,
                agent_id=stamp.agent_id,
                agent_type=stamp.agent_type if run_id else None,
                run_id=run_id,
                permission_mode=stamp.permission_mode,
                deadline=time.monotonic() + CALL_DEADLINE_S,
            )
        )
        try:
            return await call_next(context)
        finally:
            CURRENT_TURN.reset(token)

    async def _writer_turn(
        self,
        svc: Services,
        name: str,
        args: dict[str, Any],
        stamp: stamps.Stamp,
        record: dict[str, Any] | None,
    ) -> tuple[str, str]:
        """A subagent's call: only nh:cell-writer, working for an open nh:qa-cell run of the
        current human message, may add, edit or wait; only wait when that message explains,
        plans or asks (E109). Returns (the run's turn, its run id)."""

        def refuse(code: str, detail: str = "") -> NhError:
            verb = "waited" if name == "nh_run" else _VERBS.get(name, "written")
            return NhError(code, detail, next_step=RETURN_TO_WORKFLOW, verb=verb)

        if stamp.agent_type != turn_record.WRITER_AGENT:
            raise NhError("E103")
        if name == "nh_undo":
            raise refuse("E103", "- nh:cell-writer can't undo; only the main conversation can.")
        if name == "nh_run" and (args.get("mode") or "run") != "wait":
            raise refuse(
                "E103",
                '- nh:cell-writer may only wait for its cell (mode="wait"); writing a cell runs it.',
            )
        # Its first call can beat the PostToolUse hook that records the run: re-read up to 2 s.
        run = await asyncio.to_thread(
            turn_record.run_for_agent,
            svc.layout,
            stamp.session_id,
            stamp.agent_id or "",
            wait_s=turn_record.META_WAIT_S,
        )
        if run is None:
            raise refuse(
                "E103",
                "- This agent is not inside nh's qa-cell workflow: no nh:qa-cell run of this "
                "session, or more than one, lists it.",
            )
        if not turn_record.is_own_run(run):
            raise refuse(
                "E103",
                "- Only nh's own nh:qa-cell workflow, launched by name, may write; this run's "
                "script is not nh's.",
            )
        turn_id = run.get("turn_id")
        if (
            not turn_record.run_open(run, time.time())
            or record is None
            or not turn_id
            or turn_id != record["turn_id"]
        ):
            raise refuse("E107")
        if _is_write(name, args) and turn_record.no_write_mode(record, turn_id):
            raise refuse("E109")  # an explain, plan or ask message: the writer may only wait
        return turn_id, str(run["run_id"])


# Every tool the gate stamps, as the verb of its "Not <verb>:" refusals.
_VERBS = {"nh_add_cell": "written", "nh_edit_cell": "written", "nh_run": "run", "nh_undo": "undone"}


def _is_write(name: str, args: dict[str, Any]) -> bool:
    """Changes the notebook or runs a cell: E108 holds it while nh:qa-cell writes this turn's
    cell, and E109 refuses it in an explain, plan or ask message. ``nh_run`` wait and interrupt
    only follow a running cell."""
    if name == "nh_run":
        return (args.get("mode") or "run") not in ("wait", "interrupt")
    return name in _VERBS


def _guarded(fn: Callable[..., Awaitable[ToolResult]]) -> Callable[..., Awaitable[ToolResult]]:
    """Nothing may escape a tool: Claude Code never restarts a crashed stdio server.

    A refusal also carries the call's lead lines (``CALL_LEAD``): news like "NEW kernel" is
    recorded when found, so a call that is then refused must still deliver it. A refusal to
    nh:cell-writer also ends with ``WRITER_LINE``.
    """

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> ToolResult:
        lead: list[str] = []
        token = CALL_LEAD.set(lead)
        try:
            return await fn(*args, **kwargs)
        except NhError as exc:
            _with_lead(_for_writer(exc), lead)
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            log.exception("internal error in %s", fn.__name__)
            error = NhError("E199", detail=scrub(f"{type(exc).__name__}: {exc}")[:300])
            raise _with_lead(_for_writer(error), lead) from None
        finally:
            CALL_LEAD.reset(token)

    return wrapper


def _for_writer(error: NhError) -> NhError:
    """nh:cell-writer's refusal ends with WRITER_LINE, after Next:, unless it already says so.
    E120 is the exception: the writer fixes the code and calls again."""
    turn = CURRENT_TURN.get()
    if turn is None or turn.agent_type != turn_record.WRITER_AGENT or error.code == "E120":
        return error
    if RETURN_TO_WORKFLOW not in str(error):
        error.args = (f"{error}\n{WRITER_LINE}",)
    return error


def _with_lead(error: NhError, lead: list[str]) -> NhError:
    """The refusal with ``lead`` above its first line, like a result's lead lines."""
    if lead:
        error.args = ("\n".join([*lead, str(error)]),)
    return error


def _attach_file_log(layout: Layout) -> None:
    if any(getattr(h, "_nh_file", False) for h in log.handlers):
        return
    try:
        layout.logs.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(layout.logs / "gateway.log", encoding="utf-8")
    except OSError:
        return
    handler._nh_file = True  # type: ignore[attr-defined]
    handler.addFilter(_TokenScrub())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)


def create_server(project: Path | None = None, backend: NotebookBackend | None = None) -> FastMCP:
    runtime = _Runtime(project, backend)
    svc = runtime.services()
    approve = bool(svc.config()["approval"]["approve_before_run"]) if svc else False
    metas = tool_meta(svc.project if svc else None, approve)

    @asynccontextmanager
    async def lifespan(_server: FastMCP):
        try:
            yield {}
        finally:
            if runtime._services is not None:
                aclose = getattr(runtime._services.backend, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:
                        log.exception("backend shutdown failed")

    mcp = FastMCP(
        "nh",
        instructions=INSTRUCTIONS,
        middleware=[TurnGate(runtime)],
        lifespan=lifespan,
        mask_error_details=True,
    )

    @mcp.tool(
        name="nh_inspect",
        description=DESCRIPTIONS["nh_inspect"],
        output_schema=None,
        meta=metas["nh_inspect"],
        annotations=ToolAnnotations(
            read_only_hint=True, idempotent_hint=True, open_world_hint=False
        ),
    )
    @_guarded
    async def nh_inspect(
        view: Annotated[
            str, Field(description="status | overview | outline | vars | var | cell | intents")
        ] = "overview",
        name: Annotated[str | None, Field(description="Variable name for view='var'.")] = None,
        cell_id: Annotated[
            str | None, Field(description="Cell id for view='cell', or to center the outline.")
        ] = None,
        rows: Annotated[
            int | None,
            Field(
                description="Rows of a dataframe head for view='var' (default [inspect].head_rows, max 20)."
            ),
        ] = None,
        notebook: Notebook = None,
        ctx: Context | None = None,
    ) -> ToolResult:
        return await inspect_tool.inspect_notebook(
            runtime.require(),
            ctx,
            view=view,
            name=name,
            cell_id=cell_id,
            rows=rows,
            notebook=notebook,
        )

    @mcp.tool(
        name="nh_add_cell",
        description=DESCRIPTIONS["nh_add_cell"],
        output_schema=None,
        meta=metas["nh_add_cell"],
        annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False),
    )
    @_guarded
    async def nh_add_cell(
        title: Annotated[
            str, Field(description="What the cell does, at most 8 words, plain text.")
        ],
        notes: Annotated[
            list[str] | str, Field(description="2-5 plain bullets: what the cell does and why.")
        ],
        intent: Annotated[str, Field(description="The user's ask in one line.")],
        code: Annotated[str, Field(description="The cell's Python code.")],
        after_cell_id: Annotated[
            str | None, Field(description="Insert after this cell. Default: at the bottom.")
        ] = None,
        notebook: Notebook = None,
        ctx: Context | None = None,
    ) -> ToolResult:
        return await write_tool.add_cell(
            runtime.require(),
            ctx,
            title=title,
            notes=notes,
            intent=intent,
            code=code,
            after_cell_id=after_cell_id,
            notebook=notebook,
        )

    @mcp.tool(
        name="nh_edit_cell",
        description=DESCRIPTIONS["nh_edit_cell"],
        output_schema=None,
        meta=metas["nh_edit_cell"],
        annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False),
    )
    @_guarded
    async def nh_edit_cell(
        cell_id: Annotated[str, Field(description="The cell to change.")],
        code: Annotated[str, Field(description="The new code for the whole cell.")],
        base_sha: Annotated[
            str | None,
            Field(description="sha from nh_inspect(view='cell') for cells you did not just write."),
        ] = None,
        title: Annotated[str | None, Field(description="New note title (nh cells only).")] = None,
        notes: Annotated[
            list[str] | str | None, Field(description="New 2-5 note bullets (nh cells only).")
        ] = None,
        intent: Annotated[
            str | None,
            Field(description="The user's ask in one line (required if the cell has none)."),
        ] = None,
        notebook: Notebook = None,
        ctx: Context | None = None,
    ) -> ToolResult:
        return await write_tool.edit_cell(
            runtime.require(),
            ctx,
            cell_id=cell_id,
            code=code,
            base_sha=base_sha,
            title=title,
            notes=notes,
            intent=intent,
            notebook=notebook,
        )

    @mcp.tool(
        name="nh_run",
        description=DESCRIPTIONS["nh_run"],
        output_schema=None,
        meta=metas["nh_run"],
        annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False),
    )
    @_guarded
    async def nh_run(
        cell_id: Annotated[str, Field(description="The cell to run, wait for, or interrupt.")],
        mode: Annotated[str, Field(description="run | wait | interrupt")] = "run",
        notebook: Notebook = None,
        ctx: Context | None = None,
    ) -> ToolResult:
        return await run_tool.run_cell(
            runtime.require(), ctx, cell_id=cell_id, mode=mode, notebook=notebook
        )

    @mcp.tool(
        name="nh_undo",
        description=DESCRIPTIONS["nh_undo"],
        output_schema=None,
        meta=metas["nh_undo"],
        annotations=ToolAnnotations(destructive_hint=True, open_world_hint=False),
    )
    @_guarded
    async def nh_undo(
        cell_id: Annotated[
            str | None, Field(description="The cell to undo. Default: nh's last change.")
        ] = None,
        force: Annotated[
            bool, Field(description="Only after the user confirms overwriting their own edits.")
        ] = False,
        notebook: Notebook = None,
        ctx: Context | None = None,
    ) -> ToolResult:
        return await undo_tool.undo(
            runtime.require(), ctx, cell_id=cell_id, force=force, notebook=notebook
        )

    return mcp


def main() -> None:
    level = os.environ.get("NH_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.WARNING),
        format="nh %(levelname)s %(name)s: %(message)s",
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(_TokenScrub())
    server = create_server()
    server.run(transport="stdio", show_banner=False)
