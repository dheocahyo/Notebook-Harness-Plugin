# Replies

The reply is where the user builds understanding of the cell, so it has no
length limit. The notebook stays short; the chat carries the reasoning.

## By status

| Status | The reply says |
|---|---|
| ok | what the cell does; why this approach; judgment calls they may want different; the real numbers, surprises first; failed attempts and what changed; one proposed next cell as a title they can approve with "go" |
| error, retries left | nothing yet: fix the same cell with `nh_edit_cell`. Once it runs, say what failed first and what you changed, then the ok contract |
| error, no retries left | the failing code (quoted), what Python said, the likely cause, one fix; offer **undo**; wait |
| running | it is still running and its output appears in JupyterLab; you may `nh_run(mode="wait")` while waits remain; then stop. Add nothing |
| queued | as running, but nothing of it ran yet: it waits behind a cell already running in the kernel (the user can stop that one in JupyterLab). Add nothing |
| aborted | "The cell ahead of yours failed; nothing of yours ran." Name that cell; this used no retry |
| timeout | it hit the time limit (`[exec] hard_timeout_s`); say what finished and suggest a faster approach (a sample) or a longer limit; add nothing |
| interrupted | it was stopped before it finished, maybe on purpose: not a failure to fix. Say what finished and what did not; ask before re-running or changing it (nh refuses with `E117` until the user says so) |
| deleted | the user deleted it in JupyterLab while it ran: take that as a no. Never re-add, rebuild or re-run it; say what ran first (its variables may still be in the kernel) and ask what they want instead |
| lost | the kernel restarted or went away while it ran: there is no result and earlier variables may be gone. Tell the user (Kernel → Restart Kernel and Run Up to Selected Cell rebuilds them); add nothing |
| conflict (reminder only) | nh did not run it: the user typed into the cell before it started. Show their change (`nh_inspect(view="cell")`) and ask before running it (nh refuses with `E118` until they say so) |
| refused | the refusal's first line in plain words, then do what its `Next:` line says |

Always:
- Name cells by title and `[n]`: "Drop rows with missing price" [4]. Never
  `nh-` ids, never line numbers; quote the code instead.
- Say where the cell landed ("at the bottom, below "Load raw data and check
  schema" [1]"): JupyterLab does not scroll to new cells.
- If a line sits above the result's usual first line ("Kernel ≠ notebook: …"
  or "NEW kernel: …"), lead your reply with it.
- Numbers come from the output. Never invent or round away a surprise.

## Example: ok

> Added "Drop rows with missing price" as [2], below "Load raw data and check
> schema" [1]. It keeps only the orders that have a price, in a new frame
> `df_clean`, so `df` stays as loaded.
>
> Why: a missing price would drag the regional means toward zero if it were
> filled with 0, and there are too few rows to impute sensibly.
>
> Judgment call: I dropped rather than imputed. With 43 orders, filling 6
> prices with a regional median would invent 14% of the data.
>
> Numbers: 6 of 43 rows (14.0%) had no price; `df_clean` has 37 rows. No other
> column changed.
>
> Next: **Plot mean price per region**. Say "go".

## Reviews of the last cell

| The user says | Do |
|---|---|
| go, accept, ok, next, y | the cell stands; do the proposed step as this message's cell |
| a new instruction | the cell stands; handle the new ask |
| edit … / change … | `nh_edit_cell` on that cell; this is the message's cell |
| undo | `nh_undo`; reply as below |
| explain, `/nh:explain` | a numbered walkthrough in chat, quoting the code piece by piece: what each part does and why, with the real values from its output. Read-only: at most `nh_inspect`; nh refuses any change (E109) unless the message also asks for one ("explain and fix …": that fix is the message's cell) |
| tidy | apply the readability hints with `nh_edit_cell`; this is the message's cell |
| retry, run again | `nh_run` on that cell; it uses the message's cell |

## Undo

`nh_undo` removes the cell (or restores its old code) in the notebook. It does
not roll back the kernel. Tell the user, in plain words:
1. What was removed or restored, by title and `[n]`.
2. What still lives in the kernel: variables the removed cell created or
   changed keep their values (the result's "Kernel ≠ notebook: …" line above
   its first line names them).
3. How to rebuild the kernel from the notebook: select the last cell to keep,
   then **Kernel → Restart Kernel and Run Up to Selected Cell**.
4. Later cells now outdated (`--- stale ---`), by title and `[n]`.
5. An offer to redo the step differently.

> Removed "Drop rows with missing price" [2] and its note. `df_clean` (37 rows)
> is still defined in the kernel, because undo doesn't touch running state;
> `df` was never changed. To rebuild the kernel from the notebook, select
> "Load raw data and check schema" [1], then Kernel → Restart Kernel and Run Up
> to Selected Cell. No later cell used `df_clean`. Want the rows handled
> differently, for example filled with each region's median price?

## Drift, stale cells and the user's own edits

| Signal | What it means | Say |
|---|---|---|
| "Kernel ≠ notebook: …" above the first line | names still hold results of an undone cell | lead with it; give the menu path above |
| "NEW kernel: earlier variables are gone …" above the first line | the kernel restarted or was replaced since nh's last call | lead with it; propose re-running the cells above (the user runs Kernel → Restart Kernel and Run Up to Selected Cell, or asks you) |
| `--- kernel ---`: "The kernel runs from …, not the project env" | the notebook's kernel is not the project's environment | tell the user packages may differ; suggest Kernel → Change Kernel to the project's kernel, or `nhctl lab start` |
| `STALE` in the outline, `--- stale ---` | a cell used data that changed since it ran | name it; propose re-running it as the next step, don't do it unasked |
| `agent*` in the outline | the user edited nh's code | work from their version; mention it if relevant |
| `E141` | the user changed the cell since nh last saw it | show the change, ask before overwriting |
| `E117` | the cell was stopped before it finished; nh won't change or re-run it unasked | say what ran before it stopped; ask whether to re-run it, change it or leave it |
| `E118` | the user typed into nh's cell before it ran; nh won't run or change it unasked | show their change; ask whether to run it as it is, restore nh's version (`nh_undo`) or leave it |
| `E142` | the cell was already deleted in JupyterLab | nothing undone; say what is still in the kernel and follow `Next:` |
| `E144` | a cell the user wrote, edited without `base_sha` | `nh_inspect(view="cell")`, then pass its `sha` as `base_sha` |

To reject a cell the user can say **undo** or delete it in JupyterLab.
Ctrl+Z in JupyterLab only undoes their own typing, never nh's cells.
