"""Refusal catalogue. Every refusal says what happened, whether anything was written, and what to do next.

The first line is written for the human (Claude Code shows it in red); ``Next:`` is for the agent.
"""

from __future__ import annotations

import re

from fastmcp.exceptions import ToolError

_TOKEN = re.compile(r"(token=)[^&\s\"']+", re.IGNORECASE)

# The Next line of nh:cell-writer's refusals: its final answer goes to the nh:qa-cell workflow.
RETURN_TO_WORKFLOW = (
    "Return this refusal to the workflow as your final answer; don't retry or reply to the user."
)
# Added after Next: to a refusal of a writer's tool call (E120 excepted: fix and call again).
WRITER_LINE = "Writer: return this refusal to the workflow; don't retry or reply to the user."

CATALOGUE: dict[str, tuple[str, str]] = {
    # code: (first line, next step)
    "E101": (
        "nh can't tell which of your messages this call belongs to (its hooks didn't stamp it), so it wrote nothing.",
        "Tell the user nh's hooks are not running and suggest /nh:status. Do not retry more than once.",
    ),
    "E102": (
        "This call belongs to an earlier message, so nh wrote nothing.",
        "Stop and wait for the user's next message.",
    ),
    "E103": (
        "Only the main conversation, or nh's cell writer inside the nh:qa-cell workflow, may "
        "change the notebook; other subagents are read-only.",
        "Return your findings to the main agent; it writes the cell.",
    ),
    "E104": (
        "Plan mode: nh writes nothing.",
        "Describe the cell you would write instead of calling write tools.",
    ),
    "E105": (
        "This folder is not an nh project yet.",
        "Ask the user to run /nh:init (or /nh:init with adopt for an existing notebook).",
    ),
    "E106": (
        "This Claude Code version doesn't send prompt ids, so nh can't count cells per message.",
        "Tell the user to run `claude update` (needs 2.1.196 or newer).",
    ),
    "E107": (
        "Not {verb}: this nh:qa-cell run belongs to an earlier user message, already reported, "
        "or started over an hour ago.",
        RETURN_TO_WORKFLOW,
    ),
    "E108": (
        "Not {verb}: the nh:qa-cell workflow is writing this message's cell.",
        "Tell the user it is still writing and checking the cell; reply when its report arrives. "
        "To change course, stop it first.",
    ),
    "E110": (
        "Not written (by design): one new cell per message, and this message's cell is {cell}.",
        "Don't write more cells. Reply with the remaining steps as a numbered list and ask which to do next.",
    ),
    "E111": (
        "Not written: {cell} already had {retries} retries this message.",
        "Explain the error in plain words (failing code, what Python said, likely cause, one fix), offer undo, and wait.",
    ),
    "E112": (
        "Not written (by design): {cell} already ran OK. Changes wait for the user's next message.",
        "Report the result and wait.",
    ),
    "E113": (
        "Not written: this message's cell is {cell}; nh won't change {other} in the same message.",
        "Propose the change to {other} as the next step and wait.",
    ),
    "E114": (
        "Not run: re-running an older cell counts as this message's one action, and it is used.",
        "Ask the user before re-running it next message.",
    ),
    "E115": ("Not undone: undo limit for this message reached.", "Tell the user and wait."),
    "E116": (
        "Not waited: the wait limit for this message is reached; the cell keeps running in the notebook.",
        "Tell the user it is still running and that outputs appear in JupyterLab. Stop.",
    ),
    "E117": (
        "Not {verb}: {cell} was stopped before it finished, so nh asks before changing or re-running it.",
        "Tell the user what ran before it stopped, and ask whether to re-run it, change it or leave it. Wait.",
    ),
    "E118": (
        "Not {verb}: the user typed into {cell} before it ran, so nh asks before running or changing it.",
        "Show the user their change and ask whether to run it as it is, restore nh's version "
        "(nh_undo) or leave it. Wait.",
    ),
    "E120": (
        "Not written: the cell broke nh's hard rules.",
        "Fix every listed problem and call again. This did not use up your cell.",
    ),
    "E121": (
        "Not written: too many rejected attempts this message.",
        "Explain to the user what you are trying to write and ask how to proceed.",
    ),
    "E130": (
        "nh can't find this project's JupyterLab.",
        "Ask the user to start it with `nhctl lab start` (or /nh:init), then retry.",
    ),
    "E131": (
        "The connected JupyterLab has no real-time collaboration (jupyter-collaboration >= 5 needed).",
        "Ask the user to start the project's JupyterLab with `nhctl lab start`. nh_inspect still works.",
    ),
    "E132": (
        "Notebook not found: {notebook}.",
        "Pass notebook=<path relative to the project>. Candidates: {candidates}",
    ),
    "E133": (
        "The kernel is busy{detail}.",
        'Wait, or call nh_run(mode="wait") for a cell nh is running.',
    ),
    "E134": (
        "No usable kernel for this notebook{detail}.",
        "Ask the user to select or restart the kernel in JupyterLab, then retry.",
    ),
    "E135": (
        "nh couldn't sync with the notebook in JupyterLab.",
        "Tell the user; retry once. If it persists, ask them to reload the notebook tab.",
    ),
    "E136": (
        "nh only supports Python kernels and nbformat 4 notebooks{detail}.",
        "Tell the user this notebook can't be used with nh.",
    ),
    "E137": (
        "nh v0.1 does not support Windows.",
        "Tell the user to run Claude Code and JupyterLab under WSL.",
    ),
    "E138": (
        "This notebook is open in another Jupyter server ({url}); two servers would overwrite each other.",
        "Ask the user to close it there or stop that server.",
    ),
    "E140": (
        "Cell not found (it may have been moved, re-created or deleted in JupyterLab){detail}.",
        'Call nh_inspect(view="outline") and use a current id.',
    ),
    "E141": (
        "Not {verb}: the user changed {cell} since nh last saw it.{diff}",
        "Show the user the change and ask; retry with base_sha from nh_inspect, or force=true for undo only after they confirm.",
    ),
    "E145": (
        "Not written: {cell} is a markdown cell; nh only writes code cells.",
        "Suggest the wording and let the user change it in JupyterLab.",
    ),
    "E144": (
        "Not written: {cell} was written by the user, and nh needs the version you last looked at.",
        'Call nh_inspect(view="cell", cell_id=...) and pass its sha as base_sha.',
    ),
    "E142": (
        "Nothing undone: {cell} was already deleted in JupyterLab.",
        "Ask whether to undo the step before it ({previous}).",
    ),
    "E143": (
        "Nothing to undo in this session's recent turns.",
        "Ask the user which cell to undo (pass cell_id).",
    ),
    "E199": (
        "nh hit an internal error; nothing more was written.",
        "Tell the user; details are in .nh/logs/gateway.log.",
    ),
}


def scrub(text: str) -> str:
    return _TOKEN.sub(r"\1***", text)


class NhError(ToolError):
    """A refusal. ``detail`` fills a ``{detail}`` slot in the first line, or else gets its own
    line; ``next_step`` replaces the catalogue's Next line; ``cell_id`` goes on the machine line."""

    def __init__(
        self, code: str, detail: str = "", *, next_step: str | None = None, **fields: object
    ) -> None:
        self.code = code
        head, nxt = CATALOGUE[code]
        defaults: dict[str, object] = {
            "verb": "written",
            "detail": "",
            "diff": "",
            "cell": "the cell",
            "other": "that cell",
            "retries": "",
            "notebook": "",
            "candidates": "none",
            "url": "",
            "cell_id": "",
            "previous": "none",
        }
        defaults.update({k: v for k, v in fields.items() if v is not None})
        inline = "{detail}" in head
        if inline:
            defaults["detail"] = detail.rstrip()
        first = head.format(**defaults)
        machine = f"nh: {code}"
        if fields.get("cell_id") and "{cell_id}" not in head:
            machine += f" cell_id={fields['cell_id']}"
        lines = [first, machine]
        if detail and not inline:
            lines.append(detail.rstrip())
        lines.append(f"Next: {next_step or nxt.format(**defaults)}")
        super().__init__(scrub("\n".join(lines)))
