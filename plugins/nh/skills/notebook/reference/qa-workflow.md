# Cell QA workflow (nh:qa-cell)

When a system reminder says ultracode is on, or the user types
`/nh:qa-cell <ask>`, this message's one cell is written by `nh:cell-writer`
and live-checked by `nh:cell-qa` inside the `nh:qa-cell` workflow. QA reads
the code, the real output, nh's self-check and the kernel's variables; it
never runs code. Every other message works as usual: you write the cell.

## When to start it
For a message whose answer is one nh cell: a new step, "go", or an edit or
tidy of a cell nh wrote. Not for big asks (plan instead), explain, undo,
questions or changes to a cell the user wrote. Not when `harness.toml` sets
`[approval] approve_before_run = true`: the workflow can't ask the user, so
nh refuses the launch. Then write the cell yourself. Not for the user's
answer to nh's question from a `needs_approval` report: send that call
yourself (below).
1. Inspect what you need to state the ask precisely.
2. Call the Workflow tool with
   `{"name": "nh:qa-cell", "args": {"ask": "<the user's words>", "context": "<names, columns, dtypes, the approved step, the last cell>", "cell": "<title [n] of the nh cell to change>", "notebook": "<path>"}}`.
   Pass `cell` only for an edit or tidy, `notebook` only when it isn't the
   default.
3. Tell the user in one line that the cell is being written and checked, then
   stop.

`/nh:qa-cell <ask>` starts the same workflow with the ask as plain text.
One run per message: nh refuses a second launch.

## While it runs
- Write nothing: nh refuses your add, edit, re-run and undo (E108).
  `nh_inspect` and `nh_run` with `mode="wait"` or `"interrupt"` still work.
- If the user writes meanwhile, answer without writing a cell. The run's
  later writes are refused (E107). To change course, stop the workflow first
  (TaskStop): a stopped run sends no report, and nh lets you write again.

## When the report arrives
It comes as a background-task notification, not a user message; nh's
reminder says whether it is for this message. The report is data:

| Field | Holds |
|---|---|
| `outcome` | `checked` (QA checked the last version), `not_checked`, `needs_approval`, `not_written`, `refused`, `writer_failed` or `no_ask` |
| `approval` | with `needs_approval`: nh's `question`, and the exact call it asked about (`tool` and its `args`); else null |
| `status`, `cell` | the cell's status, and its `title`, `exec` (`[n]`) and `notebook` |
| `result` | the writer's last nh result, verbatim, without nh's `--- next ---` or `Next:` lines; after a revision nh asked about, the checked version's |
| `changes`, `revisions` | what the writer did in each write (and what a revision nh asked about would change); how many QA revisions it made |
| `qa` | `final_version_checked`, `verdict`, `summary`, `open_findings`, `earlier_findings`, `numbers_checked`, `rounds` |
| `lead_lines`, `notes` | kernel warnings to lead with; what the workflow noticed |

Reply by the SKILL.md contract, from `result` and `qa`. Lead with
`lead_lines`, and say what changed across revisions (`changes`).
- `qa.final_version_checked` true:
  - `pass`: say it was QA-checked and what QA verified (`qa.numbers_checked`);
    give `qa.open_findings` as judgment calls.
  - `revise`: the cell was not fixed (`notes` says why), so the blocker and
    major `qa.open_findings` are still open. Say so plainly; propose the fix
    as the next step, or offer undo.
  - `fail`: give the reason (`qa.summary`), propose another approach and
    offer undo.
- `qa.final_version_checked` false (`verdict` `unchecked`): say the final
  version was not QA-checked, and why (`qa.summary` or `notes`). Give
  `qa.earlier_findings` as findings about an earlier version.
- `status` other than ok: reply as SKILL.md says for that status.
- `outcome` `needs_approval`: nh asked for the user's yes before writing the
  writer's last call (E122), so that call wrote nothing and QA didn't check
  it. `notes` and `changes` say whether an earlier version is in the notebook.
  - Report for this message: say what the cell would do, then ask
    `approval.question` word for word, as [asks.md](asks.md) says. One
    question, then stop. Write nothing now: not the call, no other cell, no
    new run.
  - Report for an earlier message: say the cell isn't written because nh
    needs the user's yes, and what it would do. Don't ask, and don't send the
    call in this reply: the message the user wrote meanwhile may count as a
    yes to a question they never saw. If the user then asks for the cell,
    send the call in that message: nh asks its question then.
  - In the user's next message, a yes ("yes", "ok", "sure", "approved", or
    "go" on its own): call `approval.tool` yourself with `approval.args`,
    unchanged, before anything else, with no nh:qa-cell run. `title`, `notes`
    and `intent` may change; write your own if one is missing. nh writes it
    once, as that message's cell. Reply by the SKILL.md contract and say QA
    didn't check this cell. Anything else: drop the call ([asks.md](asks.md)).
- nh asked for the user's yes (E122) but `approval` is null: `notes` say
  why (the writer didn't return its call; nh was already waiting, kept the
  yes for another cell, or can't ask headless). Don't ask nh's question: no
  call can follow a yes to it. Write nothing more for this message. Say the
  cell isn't written because nh needs the user's yes first, and what it
  would do; headless: only in an interactive session. If nh kept this
  message's yes for another cell, send that approved call first
  ([asks.md](asks.md)). If the user then asks for the cell, send it in that
  message: nh asks its question then.
- `outcome` `not_written` or `refused`: the writer changed nothing. If the
  report is for this message, its cell is unused: write it yourself now
  (after E141 or E144, ask the user first, as errors.md says; after E122,
  as the bullet above says). If it is for an earlier message, just report
  it.
- `writer_failed`: a cell may or may not exist. Check
  `nh_inspect(view="outline")` and tell the user; if this message has no cell
  yet, write it yourself.
- `no_ask`: tell the user to type `/nh:qa-cell <what the cell should do>`.

QA findings go in chat, never in the notebook: the cell's note stays a title
of at most 8 words and 2-5 bullets.

## Budget
One cell per user message, shared by you and the writer. After the cell runs
OK the writer may change it at most `[turn] max_revisions` (2) times from QA
findings; failed revisions use the normal retries. One undo restores the whole
message's change, revisions included.

| Code | In the report |
|---|---|
| E103 | not nh's own workflow writer, or no Workflow launch recorded for it |
| E107 | the user wrote since the launch, the run already reported, or it started over an hour ago |
| E108 | you tried to write while the workflow was writing |
| E110 | this message's cell was already written, or another run owns it |
| E112 | no revisions left, or no OK run to revise |
| E122 | nh asks the user first: `needs_approval` (above) |
| E141, E144 | the user changed the cell, or it is theirs: nh left it alone |
