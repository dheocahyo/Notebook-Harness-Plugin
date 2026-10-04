# C6a: paused work in progress

Not reviewed to the end and not fixed. Parked here on 2026-10-04 while CI is blocked, so it survives the session container. Nothing on this branch is meant to be merged as is.

## Where it stopped

- Base: release/0.2.0 at 04926a3.
- Done: the implement agent (code, tests, docs, design §6.3; it ran its gates) and two of three reviews (grant, contract).
- Not done: the tests review, the fixes for the findings below, the independent verify, the gates on the final state, the integration tests, and the commit to release/0.2.0.
- To resume: on release/0.2.0, `git cherry-pick -n wip/c6a`, then `git rm -r --cached docs/plans/wip-c6a` and delete the folder once read. Rerun the tests review, then the fix and verify phases with these findings as input.

## Implementer report

### summary

C6a is built and left uncommitted on release/0.2.0 (HEAD 04926a3); nothing in dev/sandbox was touched. Item by item:
(1) Design: §6.3 is written, and §6.0 b and e changed only where C6 makes a detail concrete.
(2) The grant: no pending question is recorded for a batch ask. The first gated call of a message (add, edit or re-run) decides the batch once, inside svc.locks.hold. The new tools/batch.py decide() stores it in TurnState.batch_total: None until decided, 0 for no batch, otherwise min(n, max_batch). A batch is granted only when all of these hold: the call's turn is the record's turn (new turn_record.approved_batch), the answer is yes, prev_request is a batch, no question of the previous message is pending (first ask wins), and the session is not headless. A grant is used once, by its yes message.
(3) E110's cap is now `state.batch_total or max_code_cells`. After a full batch, E110 adds the line "- The approved batch's N steps are written; ...".
(4) Results: machine_line shows `turn=k/N batch`, and render.next_block takes a `batch=(step,total,stop)` argument with texts for: going on, last step, stopped by check this, stopped by error, other statuses, and an OK result after a stop. The writer's next block is unchanged.
(6) Stop rules: report_run, through batch.reported, sets the cell's status and the stop together when the status isn't ok (running and queued included) or check_this is non-empty. Any E120/E121/E122/E124/E125 also stops the batch, via the batch.stops context manager; E125 goes through refused_early under the new NotebookLocks.hold_turn. That refusal gets STOP_LINE, and E120/E121/E125 get REFUSED_NEXT in place of "call again". A new cell while an earlier step isn't ok also stops it. After a stop, every add, edit and re-run gets E123, checked by batch.enter right after review_earlier_cells. E123 is in policy/errors.py, with rows in errors.md and docs/troubleshooting.md. `[turn] max_batch = 5` is in defaults.toml (with its comment) and docs/harness-toml.md; config.load accepts only an integer >= 1 and otherwise reads 5 and reports "turn.max_batch must be an integer >= 1".
Tests cover the batch kind of the approval matrix for the main conversation, the stop rules, the cap, single use, E122 inside a batch, first ask wins, headless, the E110/E123 texts, the next blocks, the config check and approved_batch. The writer column is left for C6b.

### design_section

"### 6.3 /nh:plan and the approved batch" sits between §6.2 and §6.4 and covers all of C6, split into C6a, C6b and C6c with a files table for each.
It contains:
- the flow per message type;
- plan mode writing nothing (C2's E109 already covers plan and ask turns);
- INSTRUCTIONS and tool descriptions unchanged, so no Gs;
- the item-2 decision: no pending question is recorded for a batch ask, because the ask turn never reaches the ledger (E109) and the record's request comes back as the next message's prev_request;
- the grant, with its batch_total None/0/N table and five conditions, single use, and locking (hold and hold_turn);
- E110's cap and its full-batch line, plus the rule that each new step needs the earlier steps OK;
- the stop-rules table, what does not stop a batch, and how an E12x stop changes the refusal text;
- E123's texts and the gate order;
- the per-cell machine line and the next-block table;
- how batches compose with C5 cell approvals: an E122 inside a batch records its question and stops the batch, the next yes grants that call alone as the message's one cell, first ask wins, headless gets no batch;
- the events batch_granted, batch_not_granted and batch_stopped, the config and the docs;
- C6b's design: the reminder parts, workflow_guard's N launches, per-slot writer runs and the writer test column;
- C6c's design: the plan skill, planning.md's batch path, and the three evals with notes on history_file;
- known gaps and the C6a test list.
§6.0 b now says a batch also needs no pending question of the previous message and an interactive session, that no pending question is recorded for a batch ask, and spells out batch_total and batch_stop concretely. §6.0 e now gives max_batch's validation.

### tests_run

- Targeted tests while building: tests/gateway/test_approvals.py batch section 33 passed; test_gateway.py 34 passed; test_r3_gateway.py 11 passed; tests/unit/test_r2_render.py 23 passed; test_config.py with test_turn_record.py 205 passed.
- After the doc rows: `uv run --project plugins/nh/server pytest -q -p no:cacheprovider tests/unit/test_skill_files.py`: 239 passed, 1 skipped (pandas-version skip).
- Full unit suite, once: `uv run --project plugins/nh/server pytest -q -p no:cacheprovider`: 6239 passed, 4 skipped, 52 deselected in 715 s, exit 0.
- I then added one assertion (an edit after a running/queued stop gets E123) to tests/gateway/test_approvals.py. That file was re-run alone: 179 passed. The full suite was not re-run after this test-only change.
- `ruff check .`: All checks passed. `ruff format --check .`: 282 files already formatted. `cd plugins/nh/server && uv run pyright src`: 0 errors, 0 warnings.
- Gc (py3.11 no-project job, five files): 3306 passed, 1 skipped.
- Gp: `claude plugin validate --strict plugins/nh` and `claude plugin validate --strict .` both passed.
- Gs not needed: no tool description changed. tests/contract (tool snapshot staleness) passed inside the full suite, and tools.snapshot.json is unchanged.
- `graphify update .` run after the code changes and again at the end (6328 nodes).
- Invariants: app.py, hooks, SKILL.md and dev/sandbox have no diff; no stray hooks __pycache__.

### evals

The errors.md change is model-facing: a new E123 row, and E110's row now mentions the batch's N. Cases that exercise errors.md's E12x rows: approval-network-cell (E122 mock), init-url-data (E120/E122 in its agent mock server) and error-retry (E120 in its mock server). I also ran one-cell-per-turn because the E110 row changed, though its mock never returns E110.

All ran from a clean rsync copy at scratchpad/impl/evalcopy with `claude plugin eval plugins/nh --trust-plugin --scaffold --ablation none -j 3 --threshold 0.8 --case <name> --json <out> --no-publish --keep-temp`. Two rounds of 3 per case, all clean:
- approval-network-cell r1 [1,1,1], r2 [1,1,1], 0 aborted
- init-url-data r1 [1,1,1], r2 [1,1,1], 0 aborted
- error-retry r1 [1,1,1], r2 [1,1,1], 0 aborted
- one-cell-per-turn r1 [1,1,1], r2 [1,1,1], 0 aborted
Nothing failed.

JSON results are in /tmp/claude-0/-home-user-Notebook-Harness-Plugin/7418c2d8-0b1e-58d2-873f-2c6f1cdc8600/scratchpad/impl/evals/, named <case>-r1.json and <case>-r2.json, with logs beside them.

None of the 24 traces shows a Read tool call on reference/errors.md (the path appears only through the skill's link), so these cases don't actually load the edited rows. No eval exercises E123 yet; that is C6c's batch-asks-once and batch-stops-on-check-this. I removed the kept /tmp/claude-eval-* dirs from my runs and the copy's evals/results.

### open_issues

- Whether E120 and E121 should stop a batch is left to V14. It follows plan D3's "any E12x" and is strict: a lint slip ends the batch. Exempting them is a one-line change, recorded under Known gaps.
- C6b is not built yet: the reminder's ask-turn and batch parts, workflow_guard's N launches, per-slot writer runs and the writer column of the batch matrix. Until then, under ultracode a batch's first step takes the message's one workflow run.
- The gateway can't see what Claude actually asked, so a yes to a different question still grants the batch the user requested (Known gaps).
- No eval covers E123 or the batch texts yet; that is C6c.
- nh_add_cell's tool description still says "Once per user message". Per the design this is deliberate: the batch's next blocks carry the instruction, so the snapshot doesn't change.
- The full suite ran before the last one-assertion test addition; that file passed on its own afterwards.

### deviations

- Beyond the plan's grant rule (answer yes and prev_request set), I added two conditions: no question of the previous message may be pending (first ask wins, so one yes answers one question) and the session must not be headless. Both are recorded in §6.3 and §6.0 b.
- batch_total has three states (None = not decided yet, 0 = no batch, N) so the batch is decided once by the message's first gated call, and a cell grant used later in the message can't open one.
- I added NotebookLocks.hold_turn (turn lock only) so that E125, which is refused before the notebook is resolved, can stop a batch under a lock.
- I added three events: batch_granted, batch_not_granted (reason pending or headless) and batch_stopped (reason: the status, check_this, not_ok, or the refusal's code).
- I added one stop rule the plan does not list: a new nh_add_cell while an earlier claimed step's status isn't ok stops the batch with E123 (reason not_ok). It covers parallel calls; in-order calls never reach it.
- While a batch goes, report_run leaves the status to batch.reported, which sets it together with the stop once check_this is known, so a parallel call can't see a step OK before its check is in.
- E123 also applies to retries, re-runs and a writer's revisions after a stop, not only to new cells. A writer's E123 Next is RETURN_TO_WORKFLOW.
- E120, E121 and E125 refusals that stop a batch get a different Next, `batch.REFUSED_NEXT` (don't call again), because their usual Next says to fix and call again.
- errors.md and troubleshooting.md: besides the E123 rows, E110's row now says a yes to 'run the next N' allows N cells. I ran one-cell-per-turn for that change too.
- Extra tests in tests/unit/test_turn_record.py (approved_batch) and an E125-outside-a-batch test in test_gateway.py.
- The writer path in C6a only gets the cap and E123: refuse_other_run (one writer run per message) stays until C6b's per-slot runs.

### files_changed

- /home/user/Notebook-Harness-Plugin/docs/design.md
- /home/user/Notebook-Harness-Plugin/docs/harness-toml.md
- /home/user/Notebook-Harness-Plugin/docs/troubleshooting.md
- /home/user/Notebook-Harness-Plugin/plugins/nh/skills/notebook/reference/errors.md
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/_shared/turn_record.py
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/config.py
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/defaults.toml
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/policy/errors.py
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/policy/turn.py
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/render.py
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/tools/batch.py (new)
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/tools/common.py
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/tools/run.py
- /home/user/Notebook-Harness-Plugin/plugins/nh/server/src/nh_gateway/tools/write.py
- /home/user/Notebook-Harness-Plugin/tests/gateway/test_approvals.py
- /home/user/Notebook-Harness-Plugin/tests/gateway/test_gateway.py
- /home/user/Notebook-Harness-Plugin/tests/gateway/test_r3_gateway.py
- /home/user/Notebook-Harness-Plugin/tests/unit/test_config.py
- /home/user/Notebook-Harness-Plugin/tests/unit/test_r2_render.py
- /home/user/Notebook-Harness-Plugin/tests/unit/test_turn_record.py

## review:grant: 5 findings

### F1 (major) plugins/nh/server/src/nh_gateway/tools/common.py

**Issue.** Two nh_add_cell calls sent in parallel get past the batch's 'check this' stop. The design (§6.3, "Each new step needs the steps before it OK" and the stop-rules bullet) says a report in a going batch sets the cell's status and its stop together, so a parallel call "sees running, and stops the batch". That is false. track()'s done callback record_finish (common.py ~L286-295) writes state.status[uid] = 'ok' as soon as the execution ends. That happens before report_run's after-probe and before batch.reported. The guard in report_run (`if not batch.going(state)`) only skips its own write; the callback has already made it. So a parallel add that takes the turn lock in that window passes batch.enter(new_cell=True), because every claim reads 'ok', and writes the next step. Its cell then runs, so the earlier step's after-probe finds the kernel busy and returns nothing. check_this ends up empty and the batch never stops for that step at all.

**Evidence.** Scratch tests are in /tmp/claude-0/-home-user-Notebook-Harness-Plugin/7418c2d8-0b1e-58d2-873f-2c6f1cdc8600/scratchpad/review-grant/test_grant_holes.py. Run from the repo with `PYTHONPATH=<repo>:<repo>/plugins/nh/server/src uv run --project plugins/nh/server pytest -q -p no:cacheprovider -c pytest.ini --rootdir . -s <file> -k <name>`.

(1) test_parallel_steps_with_no_delay: plain FakeBackend timing, no patches. The sequence is ask_and_answer, then STEPS[0], then asyncio.gather(add CHECKED, add STEPS[1]). It failed 3 runs out of 3. All 3 cells were written ('turn=3/3 batch' on both results), events batch_stopped == [], and CHECKED's result was 'ran ok in 0.0s.' with no check-this.

(2) test_parallel_steps_probe_log: logged the probes during the same race: ('vars', ['df','expensive'], 'KernelBusy: the kernel is running a cell'). CHECKED's after-probe ran while step 3's cell was running.

(3) test_parallel_step_after_a_check_this_step_finishes_is_written: deterministic version. The 'vars' probe is slowed by 1 s (a kernel round trip), and the second add starts once the ledger file shows the step-2 claim's status 'ok', while the first call is still not done. Step 3 was 'Added "Total price by region" [3] ... turn=3/3 batch', and after that came batch_stopped [{'step': 2, 'reason': 'check_this'}]. The existing test_a_parallel_step_waits_for_the_last_ones_report passes only because exec_delay_s=0.3 keeps the first cell running when the second call checks.

(4) test_parallel_steps_with_the_fix: monkeypatched record_finish so it leaves a going batch's claimed, unreported cell at its previous status. With that patch the parallel call gets 'Not written: the approved batch stopped at step 2 of 3 ...' / 'nh: E123', the stop is (2, 'not_ok'), and 2 cells are written.

**Fix.** Keep a going batch's claimed cell from reading 'ok' until batch.reported has run. One way: in record_finish, when batch.going(state) and record.uid is in state.claims and not record.reported, leave state.status[uid] unchanged (batch.reported writes it together with the stop). Another way: give TurnState a 'checked' list that batch.reported appends to, and make batch.enter(new_cell=True) require every claim to be in it as well as 'ok'. Add a regression test to test_approvals.py: exec_delay 0, CHECKED and the next step sent in parallel; expect E123 'not_ok' and no third cell. Fix the §6.3 text that says the parallel call 'sees running'. Optional hardening: in a going batch, treat a failed after-probe on an 'ok' result as a stop (fail closed), because an empty check_this then means 'not checked', not 'fine'.

### F2 (minor) plugins/nh/server/src/nh_gateway/tools/write.py

**Issue.** In C6a, one nh:qa-cell run's writer can write every step of an approved batch. refuse_other_run (write.py ~L253) refuses only a different run, and E110's cap for the writer is now batch_total. Before C6a, the cap of 1 limited a run to one new cell. The writer is now held to one cell only by its prompt. §6.3 puts the per-run rule ('the run owns no cell of the message yet' -> E110 'This nh:qa-cell run already wrote its step's cell.') in C6b. Neither §6.3's Known gaps nor the implementer's report says that until then a single run (one QA pass) can write all N cells.

**Evidence.** test_one_writer_run_writes_every_step_of_the_batch, using test_approvals.add(nh, 'writer', 'p2', 'wf_run-1', **step) for STEPS[0..2] after ask_and_answer. All three were written by the same run: 'turn=1/3 batch ... revisions=0/2', 'turn=2/3 batch', 'turn=3/3 batch'. cells: 3.

**Fix.** Either add the one-cell-per-run check now: in add_cell, for a writer with state.writer_run == turn.run_id and a claim already owned by that run, raise E110 with RETURN_TO_WORKFLOW (C6b's per-slot model replaces it). Or record this in §6.3 Known gaps as an interim state between C6a and C6b, so C6a is not committed on its own without that note.

### F3 (minor) plugins/nh/server/src/nh_gateway/tools/write.py

**Issue.** In a full batch, E110's first line contradicts its own detail line. e110() keeps the catalogue head 'one new cell per message, and this message's cell is {cell}', with cell = state.claims[0] (via _claimed), and then adds FULL_LINE saying N steps were written. The model is told both that one cell is allowed and that step 1 is 'this message's cell'.

**Evidence.** test_e110_head_in_a_full_batch printed:
'Not written (by design): one new cell per message, and this message's cell is "Load sales data" [1].' / 'nh: E110' / '- The approved batch's 3 steps are written; the rest of the plan waits for the user's next message.' / 'Next: Don't write more cells. ...'

**Fix.** In a batch, either name the last claimed step (or 'this message's N cells') in the head, or put the batch line first and use a batch-specific head. Pin it in test_gateway. errors.md is model-facing, so run the E110 cases again (one-cell-per-turn) if its row changes.

### F4 (minor) plugins/nh/server/src/nh_gateway/policy/errors.py

**Issue.** E123's first line, batch.STOP_LINE and errors.md's E123 row all say 'nh changes nothing more this message'. nh_undo still changes the notebook after a stop, because by design it neither decides nor uses a batch. The new model-facing text is therefore false. troubleshooting.md's row already words it correctly: 'nh writes, edits and re-runs nothing more'.

**Evidence.** test_undo_after_a_stop_still_changes_the_notebook: after FAILING stopped the batch, nh_add_cell got 'Not written: the approved batch stopped at step 2 of 3; nh changes nothing more this message.' Then nh_undo('p2') succeeded: 'Removed "Price per unit" [2] and its note. The code is kept in .nh/history.' (is_error False).

**Fix.** Reword E123's head, STOP_LINE and the errors.md row to 'nh writes, edits and re-runs nothing more this message' (or 'adds, changes or re-runs no cell'). Alternatively, gate nh_undo with E123 too and say so in §6.3. Update the pins in test_approvals and test_gateway. errors.md is model-facing, so run its E12x cases again.

### F5 (minor) docs/design.md

**Issue.** A 'stop' (or 'no') typed mid-turn while an approved batch is going does not stop it at the gateway. absorbed() never applies an answer, and only a mode tightens, so later steps are still written. That follows C1's binding rule ('its yes/no is never an answer'), but a mid-turn stop is the natural tightening for a multi-cell reply, and §6.3's Known gaps does not mention it. Only the model's reading of the folded-in message stops the batch.

**Evidence.** test_a_stop_typed_mid_batch_does_not_stop_it: ask_and_answer, then STEPS[0] written, then nh.turns.prompt('p2', text='stop') (absorbed), then nh_add_cell(STEPS[1]) gave 'Added "Total price by region" [2] ... turn=2/3 batch'.

**Fix.** This is the user's call, since it touches C1's decision. One option: absorbed() records that a 'no'-set message was typed (for example `absorbed_no: true`, kept to the turn), and batch.decide/enter treats it as a stop (E123, reason 'user'). The other option: list it under §6.3 Known gaps, with the reminder's batch part (C6b) telling Claude to stop on such a message.

### What this reviewer checked and found correct

I read batch.py, the C6a diff (write.py, run.py, common.py, render.py, turn.py, turn_record.py, config.py, errors.py), design §6.0 b and §6.3, plan D3, and TurnGate/_writer_turn in app.py. Through the real hooks, the gateway and FakeBackend, I confirmed the following, either with the existing tests or with my own probes:
- The grant matrix holds: {yes, no, other, "go" alone, "go on"} x {same message, next message, two messages later}. A yes typed mid-turn is absorbed and never answers, so the ask message keeps E109. Two messages later, prev_request is gone. "go on" and "yes, but ..." are not yes (classifier checked).
- A grant is single use: a second "yes" later gets one cell and E110.
- min(n, max_batch) caps the batch, including max_batch set in harness.toml. config.load rejects bool, float and values < 1 and falls back to 5.
- First ask wins against a pending cell question of the previous message. Headless gets no batch.
- decide() runs only under the per-turn lock (hold, or hold_turn for E125). It is synchronous, so two racing first calls can't both decide.
- A gateway restart keeps the stop and the cap, because TurnState is saved to the ledger file. My probes test_a_restart_keeps_the_stop and test_a_restart_keeps_the_cap passed with a new server on the same backend.
- A notification after the reply is an alias and stays within the same N.
- nh_undo doesn't free a claim, so undo can't add steps. An edit or re-run can't add steps either: E112 for an OK claim, E113/E114 for unclaimed cells, and only the first call can claim an existing cell.
- The stops hold for error, running, queued and in-order check_this. E122 inside a batch keeps its own question (pending is recorded under the batch message's turn), stops the batch, and the next yes grants exactly that call as one cell. After a stop, E123 refuses every add, edit and re-run in that turn only, while wait and interrupt still work.
- E110, its texts and machine_line are unchanged when batch_total is 0 or None.
- The C6a files I ran (test_approvals, test_gateway, test_r3_gateway, test_explain_only, test_qa_workflow, test_r2_render, test_config, test_turn_record): 542 passed.
- The repo is untouched: git status shows the same 20 entries, and there is no hooks __pycache__. Scratch tests are only under scratchpad/review-grant/test_grant_holes.py: of 11 tests, 5 fail on purpose to reproduce F1, F2, F4 and F5, and 6 pass (the restart, notification, probe-log, E110-head print and fix-direction checks).
- I did not run the full suite, lint or evals; this was a read-only review.

## review:contract: 13 findings

### F1 (major) docs/design.md

**Issue.** §6.3 (line 970) keeps nh:cell-writer's next block unchanged inside a batch, and write.py report_run passes `batch=None if writer` (line 554). But an error result already stops the batch (batch.reported), so the writer is told to fix the cell with retries left, and its fix then gets E123. Under ultracode (C6b) every writer error in a batch costs a wasted call, and the workflow gets an E123 refusal back instead of the error result. Neither C6a nor C6b's design handles this.

**Evidence.** Scratch test cur/tests/gateway/test_zz_writer_err.py (copy of the working tree): 'run the next 3', 'yes', then a writer runs nh_add_cell(FAILING). Its next block says: '"Price per unit" [1] failed. Fix it with nh_edit_cell on the same cell (2 retries left this message) and return the new result instead; ...'. The writer's nh_edit_cell fix then gets 'Not written: the approved batch stopped at step 1 of 3; ... nh: E123 / Next: Return this refusal to the workflow ...'.

**Fix.** Add a writer row to §6.3's next-block table: in a batch, a result that stops it says a batch has no retries and the writer returns the result to the workflow (status error / check this). Then pass the stop to `_writer_next` (in C6a, or say plainly that C6b does it).

### F2 (minor) plugins/nh/server/src/nh_gateway/tools/write.py

**Issue.** In C6a, one nh:cell-writer run can write up to N cells in a batch. `batch.cap` raises E110's cap for writers as well, and `refuse_other_run` only blocks other runs. §6.3's C6b table says a run may own one step's cell. This interim state, where one QA run covers several cells, is not listed in §6.3's Known gaps or anywhere for C6a.

**Evidence.** Scratch test test_zz_batch.py case f: the writer run r1 writes STEPS[0] ('turn=1/3 batch ... revisions=0/2'), then the same run's nh_add_cell(STEPS[1]) is also written ('turn=2/3 batch'). Before C6a the second add got E110 (cap 1).

**Fix.** Until C6b, keep a writer's cap at max_code_cells (for example `batch.cap` returns max_code_cells when is_writer(turn)), or record the interim gap in §6.3 under C6a/Known gaps.

### F3 (minor) plugins/nh/server/src/nh_gateway/render.py

**Issue.** In `_batch_next`, the 'error' text without retries is used only when stop == step. An error result after the batch stopped at another step falls to the generic branch (line 797). That branch prints the plain block, which says 'Fix it with nh_edit_cell ... (2 retries left)', and then 'The approved batch stopped at step 2 of 3'. The fix it asks for gets E123. §6.3's next-block table has no row for this case.

**Evidence.** Scratch test test_zz_parallel.py: step 1 is FAILING with exec_delay 0.5, and a parallel E125 call stops the batch at step 2 (refused_early: attempt_step = len(claims)+1). Step 1's next block: '"Price per unit" [1] failed. Fix it with nh_edit_cell on the same cell (2 retries left this message), then tell the user what failed and what you changed. The approved batch stopped at step 2 of 3: say which planned steps did not run.' The following nh_edit_cell gets E123.

**Fix.** In a batch with any stop, render 'error' with the no-retry text (or with the plain block at retries_left=0), and add that row to §6.3's table.

### F4 (minor) plugins/nh/server/src/nh_gateway/tools/batch.py

**Issue.** §6.3 (line 949) says 'A refusal after the stop is E123, not this'. But E125 comes before E123 (refused_early, line 183), and `refused()` leaves the error unchanged once the batch has stopped. So after a stop, an E125 keeps its usual Next, which invites rewriting the value and calling again. Related: line 949 also says E121's Next 'says to fix and call again', but E121's Next is 'Explain to the user what you are trying to write and ask how to proceed.' (errors.py).

**Evidence.** Scratch test test_zz_e125after.py: FAILING stops the batch at step 1. Then nh_add_cell and nh_edit_cell with '[redacted:API_KEY]' both return 'nh: E125 / Next: Read the value from the environment or the project's .env without printing it, or ask the user to edit that line in JupyterLab.' There is no E123 and no stop line.

**Fix.** In refused_early, when batch_total and batch_stop are set, raise batch.stopped(...) (E123) or attach REFUSED_NEXT; otherwise document the E125 exception in §6.3. Also correct the E121 wording on line 949.

### F5 (minor) docs/design.md

**Issue.** §6.0 b is no longer exactly true. Its Locking bullet (line 575) still says 'Every grant check and slot use happens inside `svc.locks.hold`', but C6a runs the batch grant check (decide()) for E125 under the new `svc.locks.hold_turn` (§6.3 line 928, batch.refused_early).

**Evidence.** design.md:575 compared with batch.py:183-192 (`async with svc.locks.hold_turn(turn): ... decide(svc, turn, state)`) and design.md:928.

**Fix.** Update §6.0 b's Locking bullet: inside `svc.locks.hold`, or for a refusal raised before the notebook is resolved (E125), under `hold_turn`, the same per-turn lock.

### F6 (minor) docs/design.md

**Issue.** C6c's design (line 1030) says skills/notebook/SKILL.md 'gets one pointer line and stays ≤ 150 lines'. SKILL.md is already exactly 150 lines, so one more line breaks the ≤ 150 invariant. As written, the step can't be done.

**Evidence.** `wc -l plugins/nh/skills/notebook/SKILL.md` gives 150. It already points to planning.md twice (lines 98 and 150: '- [reference/planning.md](reference/planning.md): the 5-12 step plan.').

**Fix.** Have C6c extend the existing pointer at line 150 (e.g. 'the 5-12 step plan and the batch path') instead of adding a line.

### F7 (minor) plugins/nh/server/src/nh_gateway/tools/write.py

**Issue.** After a full batch, E110's first line, the red line the human sees, still reads 'one new cell per message, and this message's cell is <claims[0]>', although the message wrote N cells. Only the detail line mentions the batch. The code matches §6.3 (line 932), but the text is misleading.

**Evidence.** Scratch test test_zz_batch.py case 'after full E110': 'Not written (by design): one new cell per message, and this message's cell is "Load sales data" [1]. / nh: E110 / - The approved batch's 3 steps are written; ...', printed after cells [1]-[3] were written in that message.

**Fix.** Give E110 a batch first line (e.g. 'Not written (by design): the approved batch's N cells are written.'), or pass a `cell=` that names the batch's cells. Pin it in test_gateway.

### F8 (minor) docs/design.md

**Issue.** §6.3 says nothing about nh_undo inside a going batch, other than that it neither decides nor uses a batch. In the code, an undo of a step leaves the batch going: the undone step still counts as an OK claim, so the next step is written on top of a notebook that lacks it. The undo's own Next says 'Wait.'

**Evidence.** Scratch test test_zz_batch.py case e: step 1 is OK, then nh_undo ('Removed "Load sales data" [1] ... Wait.'), then nh_add_cell(STEPS[1]) is written as 'turn=2/3 batch' with the hint '`df` is not defined by any cell above'. The next block says 'Then write step 3 of the plan'. The ledger shows batch_stop 0 and the undone uid still with status 'ok'.

**Fix.** Decide in §6.3 whether nh_undo stops a going batch (the suggestion: yes, at the undone step, reason 'undo'). Implement it in undo.py under the lock and test it, or list the current behaviour as deliberate.

### F9 (minor) docs/design.md

**Issue.** §6.3 line 934 says that for parallel calls 'the next step waits for the last one's full report', and the test is called test_a_parallel_step_waits_for_the_last_ones_report. Nothing waits: the parallel add stops the whole batch with E123 (reason not_ok), at the earlier step, which itself was fine. That step's report then reads '... ran OK, but the approved batch stopped at step 1 of 3', and E123's Next asks for 'the error, the check this finding or nh's question', none of which exists.

**Evidence.** tests/gateway/test_approvals.py test_a_parallel_step_waits_for_the_last_ones_report asserts E123 'stopped at step 1 of 3' and batch_stopped (1, 'not_ok'). In batch.enter, the not_ok loop calls stop() and raises stopped().

**Fix.** Reword the design and the test name ('a parallel step stops the batch'). Consider stopping at the refused step (len(claims)+1) rather than at the OK step, or giving E123 a not_ok detail line.

### F10 (minor) docs/design.md

**Issue.** In C6b's reminder design, the ask-turn part reads 'for the next {k} plan steps'. A 'run steps a-b' request is recorded only as {batch, n} (intent.py _BATCH_RANGE gives n = b - a + 1), so for 'run steps 7-9', when the next step is 3, the reminder says 'the next 3 plan steps'.

**Evidence.** design.md:1008 (ASK_BATCH text); intent.py lines 138-141 return {"batch": True, "n": n} for a range, with no start step.

**Fix.** Word ASK_BATCH without 'next', e.g. 'for the {k} plan steps the user named', or record the range start in the request (RECORD_VERSION stays 2 if the field is optional).

### F11 (minor) docs/design.md

**Issue.** C6b's reminder design puts the pinned RULE ('[nh] One new cell this turn; ...') and QA_REPORT ('... write no new cell unless its writer wrote none.') in front of BATCH ('up to {k} new cells ...'), and says QA_REPORT + BATCH 'lets the next step's run launch' (line 1013), without saying which instruction wins. Also, the hooks' approved_batch() has no headless or pending check (line 1002), so BATCH shows in exactly the cases where the gateway grants nothing (batch_not_granted pending/headless). Known gaps does not list that mismatch.

**Evidence.** prompt_submit.py: RULE and QA_REPORT texts (pinned). design.md:1002, 1010, 1013, and the Known gaps at 1043ff (no entry for a hook/gateway mismatch). batch.decide emits batch_not_granted with reason pending/headless.

**Fix.** Make BATCH say it overrides the one-cell line for this reply. Have the hooks also check NH_HEADLESS (pre_tool already reads it). List the pending case under Known gaps.

### F12 (minor) docs/design.md

**Issue.** workflow_guard's design is not concrete for one case. When a grant message has used its k runs, the denial text is only 'WORKFLOW_AGAIN_REASON's batch form', which is never defined. The open-run denial, by contrast, gets its full text.

**Evidence.** design.md:1015.

**Fix.** Spell out the k-runs-used text, as for the run-open text, so C6b pins it.

### F13 (minor) plugins/nh/server/src/nh_gateway/render.py

**Issue.** When the batch's last step (k == N) has a 'check this', the stop block still tells the model to 'say which planned steps did not run', but every step of the batch ran.

**Evidence.** Scratch test test_zz_batch.py case h (max_batch = 2): step 2 of 2 with CHECKED gives 'The approved batch stops at step 2 of 2: "Keep expensive sales" [2] ran, but its result needs a look ... and say which planned steps did not run. ...'

**Fix.** Drop that clause when step == total, or give §6.3's table a last-step check-this row.

### What this reviewer checked and found correct

Read and run on the working tree, which I did not modify; scratch copies are under scratchpad/review-contract/ (head = `git archive HEAD`, cur = rsync of the working tree).
- v0.1 behaviour outside a batch is unchanged. I wrote a 23-step scenario: add ok, E110, an error, a retry edit, a re-run, check this, E120 lint, E125, E122 ask and grant, an ask message's E109, a 'no' message, running then wait, queued then wait, undo, a writer's add, second add and lint. I ran it against HEAD and against the C6a tree with uid and timings normalized. The output is byte-identical ("IDENTICAL").
- Pinned texts: `git diff` shows no change to app.py, the hooks (RULE, QA_REPORT, NO_PROMPT_ID), skills/notebook/SKILL.md (150 lines) or dev/sandbox. INSTRUCTIONS measures 2017 chars (≤ 2048). Rule 1 already ends 'Only exception: a batch or re-run list the user approved when nh asked.' The tool snapshot is unchanged.
- machine_line and next_block batch forms match §6.3's tables in real gateway output: 'turn=k/N batch'; the going block; the last-step block ending 'Do not write another cell.'; the stop blocks for check this, error and running/queued; 'ran OK, but ... stopped'. The writer's machine line also shows the batch.
- E123's first line and Next are the same in policy/errors.py, the troubleshooting.md row (quoted "Not written: the approved batch stopped at step k of N …") and the errors.md row's description. The E110 rows were updated as the design says.
- [turn] max_batch: it is in defaults.toml with the stated comment and documented in harness-toml.md. config.load behaves as §6.3/§6.0 e say: bool and string give _merge's 'must be int'; float, 0 and negative give 'turn.max_batch must be an integer >= 1'; all fall back to 5.
- approved_batch runs under Python 3.9.
- §6.3's other claims hold: C2's E109 covers plan and ask (TurnGate app.py:239, tests in test_explain_only/test_hook_workflow); workflow_guard denies launches in mode turns; qa-cell.js reads only `revisions=`, so the batch machine line is safe; context.history_file and 'Seed the workspace or conversation' match the external eval docs.
- All 9 C6 items have a design. C6b and C6c are concrete except the gaps listed (F1, F6, F10-F12).
- E12x stops: E120 and E125 as first call and mid-batch, E122 inside a batch (stop line before its own Next, pending recorded, the next yes grants that call alone), running/queued, check this, and the cap at N and at max_batch all behave as designed.
- Gates I ran: targeted pytest on test_approvals, test_gateway, test_r3_gateway, test_r2_render, test_config, test_turn_record, test_skill_files, test_explain_only, test_hook_workflow and test_hook_prompt: 814 passed, 1 skipped (pandas-version skip). `ruff check --no-cache .` passed. `ruff format --no-cache --check .`: 282 files already formatted. `pyright src`: 0 errors.
- I did not re-run the full unit suite, Gc, Gp or evals. The implementer's eval JSONs for its 4 cases × 2 rounds exist and show score 1 and passed throughout.
- No JupyterLab was started, and the repo is untouched (`git status` is the same as before; no hooks __pycache__).
