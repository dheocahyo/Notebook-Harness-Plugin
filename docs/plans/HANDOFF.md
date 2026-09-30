# v0.2 build handoff (local → cloud, 2026-09-29 23:00)

The build continues in a Claude Code cloud session. `docs/plans/` is temporary: remove it in C12.

## Status

| Commit | Chunk |
|---|---|
| c454824 | C0 design §6 skeleton, version 0.2.0, C0 spikes |
| d1cc942 | C1 turn record, classifier, ledger v2, E102 |
| be15922 | C2 explain-only (E109), `/nh:explain` |
| f47eb6f | Eval mocks' title guard (the eval CLI's regex dialect) |
| 924e3c9 | C3 redaction of what Claude sees, E125 |
| 8882db8 | The first version of this handoff |
| 1a04ea7 | C2 fix: a write fails the explain evals; marked-up "explain" messages are caught |
| d93bb5b | C3 check: 29 review fixes (no secret in diffs, cut pieces or DB URLs; safe patterns; .env parsing; fewer false positives) |

Evals at d93bb5b: 8 of 9 cases pass. error-retry fails the same way at 1a04ea7 and at v0.1.1: the model parses with `errors="coerce"` first, and the case's fixed mocks contradict the code it sent. That is fixed by the first patch below.

Two workflows were stopped for the move. Their work is saved here; both patches apply cleanly to this commit, error-retry first and then C4 (checked with `git apply`).

### 1. error-retry rework: done (reviewed, fixed, committed; the patch is removed)

- **What it does.** error-retry now uses `type: agent` mocks. A model plays the gateway from templates and data facts in `error-retry/mocks/nh/fixtures/nh-server.md`, so the answers follow the code actually sent. There are case-level nh_add_cell (expect guard kept), nh_edit_cell, nh_inspect, nh_run and nh_undo mocks.
  - The prompt asks for a strict parse (`format="%Y-%m-%d"`, no `errors="coerce"`, no try/except), and asks to leave out and name a bad order.
  - The explains-fix grader's example changed from `errors="coerce"` to "leaving order 1020 out".
  - test_skill_files.py replays every state each template covers against the real gateway, so template drift fails a test (+665 lines). The README's eval paragraph describes it.
- **Where it stands.** Implemented only. Round 1 failed; rounds 2–5 scored 1.0 on all 12 runs, none aborted. It is not reviewed yet.
- **To do:**
  1. `git apply docs/plans/error-retry-wip.patch`.
  2. Two reviewers:
     - **Validity and anti-gaming.** Is the prompt still something a real user types, or does "so a bad date makes the cell fail loudly" give too much away? Does the grader keep its meaning? Would a plugin that adds a second cell, hides the failure or never retries fail? Run one round with `--keep-temp` and read the transcripts and `mock-recordings/`.
     - **Tests and formats.** Is drift protection real (break a format in a scratch copy and see a test fail)? Are the other cases unaffected?
  3. Fix what they find, then two consecutive clean rounds of 3.
  4. Gates Gu and Gp, then the full eval suite.
  5. Read the diff and commit, deleting the patch in that commit.

### 2. C4, secret lint rules L011/L014: done (reviewed, fixed, committed; the patch and review file are removed)

- **What it holds.** The whole C4 delta after an implement agent and three reviewers:
  - design §6.7;
  - `lint/secret_scan.py` (new) and `lint/lint.py`;
  - defaults.toml, harness-toml.md, errors.md, SKILL.md, troubleshooting and the READMEs;
  - tests: test_lint_hard, test_lint_hints, test_skill_files, and the new tests/gateway/test_secret_lint.py;
  - the new eval case `secret-print-refused`.
- **Review.** `c4-review.json` has the implement summary and the 18 review findings (3 major):
  - **plan:** the no-show graders miss common ways of showing a key;
  - **bypass:** displaying `os.environ.keys()` shows values;
  - **tests-pins:** many L011 rows can be removed without a test failing.
- **Where it stands.** The fix agent was stopped partway, so some fixes may be half-done in the patch. Check every finding against the code before fixing it.
- **To do:**
  1. `git apply docs/plans/c4-wip.patch` (after the error-retry commit).
  2. Fix every real finding.
  3. `graphify update .`.
  4. Gates Gu, Gc and Gp.
  5. Ge: secret-print-refused, two consecutive rounds of 3 with every run at 1.0, then the full suite.
  6. Read the diff and commit, deleting `c4-wip.patch` and `c4-review.json` in the C4 commit.

### Cloud session status (2026-09-30), stopped at the user's request after C4

- **Committed and pushed:** 17ae694 (step 1, error-retry) and 13c01ef (C4). A PR from `release/0.2.0` to `master` is open.
- **C12's independent parts, built early and not committed:** `c12-early-wip.patch` (base 17ae694; apply with `git apply -3` after C4). It holds:
  - the a7 and a8 integration tests (`tests/integration/test_large_outputs.py`, `test_lab_restart.py`);
  - the drift.yml `file-issue` job and the ci.yml artifact path (`--basetemp "$RUNNER_TEMP/pytest-integration"`);
  - three gateway fixes the tests exposed:
    - a room save request after nh's writes (cells were lost when Lab restarted mid-run);
    - websocket-client's pure-Python UTF-8 frame check replaced (50 MB stream exec 11 s → 0.8 s);
    - `prune_outputs_dir(keep=)`;
  - the plan's V11 "trim" fallback in `exec/shaping.py`, which brought a7's 50 MB stream overhead from 2.4 s to 0.6 s p95 on this slower machine;
  - its §6.13 text in `docs/plans/c12-design-6.13.md` (inside the patch).
- **Its second review was cut off by a container restart:** `c12-early-review.json` holds two of the three reviewers' findings:
  - redaction: 3 blockers and 1 major in the trim (a pattern secret longer than the margin before the tail window; HTML over its share; joined error frames; progress-bar output emptied);
  - gateway: 8 minors.
  Rerun the tests/docs reviewer, then fix and verify, before committing it.
- **Scopes for C5–C12** are drafted in `scopes/`. `workflows/chunk.js` is the chunk workflow (implement, 2–3 reviewers, fix, verify) they were written for; its args are `repo`, `scratch`, `evalDocs`, `chunk`, `head`, `evalCases`, `scope`, `lenses` and `extraRules`.
- **Environment fixes this container needed** (not in the repo):
  - uv ≥ 0.10 (`pip install -U uv`);
  - `apt-get install rsync shellcheck`;
  - `/etc/jupyter/jupyter_server_config.json` with `{"ServerApp": {"allow_root": true}}`, because JupyterLab refuses to run as root;
  - the eval docs from https://code.claude.com/docs/en/plugin-evals.md.

  At HEAD f9af3f0 the unit suite passed (2341) and integration passed 45 of 46: one 2 s probe, `test_vars_probe_never_calls_a_user_len`, fails under load and passes alone.
- **C5 note:** `test_error_retry_mocks_are_agents_on_one_description` allows agent mocks only in error-retry. A case that needs one must generalise that test.

### 3. Then C5 → C12, per `v0.2.md`

- **C12's independent parts** can be built early in a parallel workflow and committed with C12:
  - the a7 and a8 integration tests;
  - the drift.yml `file-issue` job;
  - the ci.yml:78 artifact glob.

  Write their §6.13 design text to a side file, because later chunks insert design sections at the same place.
- `workflows/` holds the two stopped workflow scripts (error-retry and C4) as references for prompts and rules. Their paths are the laptop's, so adapt them before use.

## Standing rules from the user

- **Ultracode is on.**
  - Every chunk runs as a workflow of fewer than 10 agents: implement, then 2–3 parallel reviewers, then fix. Then gate it yourself.
  - Every agent prompt states what it may and may not skip.
  - A user message relayed mid-run means stop and return, never "skip".
- **Per chunk:**
  1. `docs/design.md` first.
  2. `graphify query` before grepping, `graphify update .` after code changes. Install it with `uv tool install graphifyy`; graphify-out/ is gitignored.
  3. Run the gates.
  4. Run the chunk's evals. Never ask, and no `--max-cost-usd`. Evals come before the commit (the user was angry that C2 was committed first).
  5. Read the full diff.
  6. Commit. The message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
  7. Push the commit to `release/0.2.0` on the GitHub remote. Never push master or tags, and never open a PR.
- **Commit rhythm.** After a chunk's gates, evals and a full diff read, commit and go straight on to the next chunk. Stop only for a user-run spike or a real user decision.
- **Running evals.**
  - Run them from a clean copy: `rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude evals/results plugins/nh/ <copy>/plugins/nh/`, then from `<copy>`: `claude plugin eval plugins/nh --trust-plugin --scaffold --ablation none -j 3 --threshold 0.8 --case '<glob>' --json <out> --no-publish --keep-temp`. The CLI refuses a plugin directory with more than 20,000 entries, and `--case` is last-wins, so use one glob.
  - Mock `expect:` regexes use a small dialect: no groups, few quantifiers.
  - Traces are deleted unless you pass `--keep-temp`.
- **Test gotchas.**
  - A stray `plugins/nh/hooks/nh_hooks/__pycache__` breaks test_hook_shim.
  - Hook p95 is inflated by load, so compare before and after interleaved.
- **If `claude plugin eval` can't authenticate:** tell the user at once and ask whether to commit without evals. Meanwhile, keep implementing the next chunk. Never log in or authenticate for the user.
- **User-run spikes:**
  - V14 after C6, V15 before C8, V9 after C10, V3 before C11.
  - Prepare each one and hand it over.
  - While waiting, keep building chunks the spike doesn't gate. C9 and C10 are gated by neither V14 nor V15.
- **Never touch `dev/sandbox`,** not even its harness.toml. Spikes run in scratch copies.
- **Length budgets:** only notebook notes have one (a title plus 2–5 bullets). Never cap code or chat length. Store no prompt text.
- **Discarded work stays discarded,** including its decisions and answers. Don't reintroduce the private-repo/credentials README paragraph.
- **Invariants:**
  - INSTRUCTIONS ≤ 2048 chars.
  - `RULE`, `QA_REPORT` and `NO_PROMPT_ID` byte-identical.
  - notebook SKILL.md ≤ 150 lines.
  - 5 tools.
  - Hook p95 < 150 ms (measure before vs after under the same load).
  - Turn overhead p95 ≤ 1.5 s.
  - E110 has exactly two exceptions.

## Environment notes

- **Gates:**
  - Gu: `uv run --project plugins/nh/server pytest -q -p no:cacheprovider` (about 9 min), `ruff check .`, `ruff format --check .`, `cd plugins/nh/server && uv run pyright src`.
  - Gc: ci.yml's py3.11 lint job.
  - Gi: `-m integration --timeout 300`.
  - Gp: `claude plugin validate --strict plugins/nh` and `.`.
- **Hooks and nhctl** are Python 3.9 stdlib; `_shared` stays 3.9-compatible.
- **Workflow results:** parse workflow output with `json.loads(t[t.find('{'):])['result']`.
