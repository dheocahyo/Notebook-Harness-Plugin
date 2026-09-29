# v0.2 build handoff (local → cloud, 2026-09-29 08:40)

The build continues in a Claude Code cloud session while the user's laptop is offline. `docs/plans/` is temporary: remove it in C12.

## Status

| Commit | Chunk |
|---|---|
| c454824 | C0 design §6 skeleton, version 0.2.0, C0 spikes |
| d1cc942 | C1 turn record, classifier, ledger v2, E102 |
| be15922 | C2 explain-only (E109), `/nh:explain` |
| f47eb6f | Eval mocks' title guard (the eval CLI's regex dialect) |
| 924e3c9 | C3 redaction of what Claude sees, E125 |

- **C4 next.** `c4-partial.patch` applies to 924e3c9. It holds an implement agent's unfinished, unreviewed work: design §6.7, `lint/lint.py`, and a new `lint/secret_scan.py`. Apply it with `git apply`, delete the file in the C4 commit, and continue from it: finish the implementation, then review, fix, gates, evals, commit.
- **Then** C5 → C12, per `v0.2.md`.
- **C12's independent parts** can be built early in a parallel workflow and committed with C12, on a separate worktree:
  - a7 and a8 integration tests;
  - drift.yml `file-issue` job;
  - the ci.yml:78 artifact glob.

  Write their §6.13 design text to a side file; later chunks insert design sections at the same place.
- **Evals have not run since v0.1.1.** On the laptop, the `claude` CLI login had expired. Run the C2 cases (`--case '*explain*'`) with C4's gates.

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
