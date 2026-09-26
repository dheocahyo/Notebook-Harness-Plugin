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
nh refuses the launch. Then write the cell yourself.
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
| `outcome` | `checked` (QA checked the last version), `not_checked`, `not_written`, `refused`, `writer_failed` or `no_ask` |
| `status`, `cell` | the cell's status, and its `title`, `exec` (`[n]`) and `notebook` |
| `result` | the writer's last nh result, verbatim, without nh's `--- next ---` or `Next:` lines |
| `changes`, `revisions` | what the writer did in each write; how many QA revisions it made |
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
- `outcome` `not_written` or `refused`: the writer changed nothing. If the
  report is for this message, its cell is unused: write it yourself now
  (after E141 or E144, ask the user first, as errors.md says). If it is for
  an earlier message, just report it.
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
| E141, E144 | the user changed the cell, or it is theirs: nh left it alone |
