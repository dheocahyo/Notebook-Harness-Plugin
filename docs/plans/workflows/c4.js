export const meta = {
  name: 'nh-v02-c4',
  description: 'nh v0.2 chunk C4: FR-14 secret lint rules L011 secret_print (hard) and L014 secret_name (hint), continuing from c4-partial.patch in the main tree; implement, review, fix, with the secret-print-refused eval run until it passes',
  phases: [
    { title: 'Implement', detail: 'one agent finishes C4 in the main tree and iterates its eval case' },
    { title: 'Review', detail: 'three reviewers: plan and eval validity, bypasses and false positives, tests and pins' },
    { title: 'Fix', detail: 'one agent applies confirmed findings and reruns the gates and the eval case' },
  ],
}

const R = args.repo
const S = args.scratch
const HEAD = args.head
const PLAN = `${R}/docs/plans/v0.2.md`
const DOCS = args.evalDocs

const RULES = `
Standing rules (binding):
- Work in the main tree ${R} (branch release/0.2.0, HEAD ${HEAD} = the C3 check commit). Your C4 changes stay unstaged: \`git -C ${R} diff\` plus untracked files is exactly the C4 delta (it includes the deletion of docs/plans/c4-partial.patch).
- A separate workflow is reworking the error-retry eval case in another worktree at the same time. Do NOT touch plugins/nh/evals/error-retry/ or the error-retry entries (DATES, DATES_FIXED, the "error-retry/..." MOCK_SCENARIOS keys) in tests/unit/test_skill_files.py, and don't be surprised if error-retry fails in a full eval run: that is known and handled elsewhere.
- Run \`graphify query "<question>"\` (from ${R}) before grepping raw files; run \`graphify update .\` from ${R} after code changes.
- You may NOT skip anything in the C4 scope below; nothing is optional (no gating spike failed for C4). If you can't finish something, say so plainly; never claim a pass you didn't see.
- You must NOT: git commit/push/stash/reset/checkout/add/worktree or anything that changes the index, HEAD or a branch; touch dev/sandbox (not even its harness.toml); change pinned text (prompt_submit RULE, QA_REPORT and NO_PROMPT_ID stay byte-identical; INSTRUCTIONS in app.py stays byte-identical, since C2 already added "or print env vars or credentials" to rule 9); cap code or chat length or pass --max-cost-usd (only notebook notes have budgets: a title plus 2–5 bullets); store any prompt text anywhere; log in or authenticate anything; kill or signal any process you did not start (never the desktop app's claude processes, never the user's jupyter-lab).
- Do not look for or read any backup or leftover of an earlier, discarded v0.2 attempt (docs/plans/c4-partial.patch is NOT one: it is this build's own C4 start, already applied).
- If a user message is relayed to you mid-run: stop and return what you have with a note; never read it as "skip".
- Sources: the code at HEAD (C1's _shared/intent.py and turn_record.py, C2's E109, C3's _shared/secrets.py with is_secret_name/name_parts and the Redactor, E125, all revised by the C3 check commit), the plan ${PLAN} (D7, D0d's code table, the chunk C4 row, "Proposed defaults"), ${R}/docs/design.md §6 (§6.0 is the shared contract and already says secret_print (L011) and secret_name (L014) are [lint.rules] keys defined in 6.7; §6.8 is redaction and says C4's L014 reuses is_secret_name).
- The lint module runs in the gateway (Python 3.11+), but CI's "lint and hook helpers on the oldest supported Pythons" job (${R}/.github/workflows/ci.yml ~L53-58) runs tests/unit/test_magics.py, test_lint_hard.py, test_lint_hints.py, test_lint_tokenize_compat.py and test_dataflow.py with \`uv run --no-project --python 3.11 --with pytest --with pytest-timeout --with pytest-asyncio --with hypothesis\`: lint.py and anything it imports (secret_scan.py included) must stay importable there (stdlib plus the gateway's own pure modules; _shared/secrets.py is stdlib, so importing it is fine). _shared code stays Python 3.9 compatible.
- Tests: \`uv run --project plugins/nh/server pytest -q -p no:cacheprovider <paths>\` from ${R} (the full suite takes ~9-15 min on this loaded machine; run targeted files while iterating). Gc: the exact ci.yml py3.11 command above, from ${R}. Lint: \`uv run --project plugins/nh/server ruff check .\` and \`ruff format --check .\`, \`cd plugins/nh/server && uv run pyright src\`. Plugin: \`claude plugin validate --strict plugins/nh\` and \`claude plugin validate --strict .\`.
- Evals (\`claude plugin eval\`; the CLI is logged in): ONLY from a clean copy under ${S}/<your key>/, never from ${R}. Make it with \`mkdir -p <copy>/plugins && rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude evals/results ${R}/plugins/nh/ <copy>/plugins/nh/\`, then from <copy> run \`claude plugin eval plugins/nh --trust-plugin --scaffold --ablation none -j 3 --threshold 0.8 --case '<case>' --json <out.json> --no-publish --keep-temp\`. Refresh the copy before every round. Results: cases[].arms.with[] runs with score, aborted, graders[{name, passed, evidence}]; --keep-temp keeps the transcripts. Scratch files go under ${S}/<your key>/ only, never in the repo.
`

const SCOPE = `
C4 scope (plan D7's L011 and L014; E125 was done in C3; design §6.7). Implement all of it.

0. Starting point: docs/plans/c4-partial.patch (an earlier implement agent's unfinished, UNREVIEWED work for this same chunk of this build, saved when the session moved; it is not a discarded attempt) is already applied, unstaged, in the tree: design §6.7, lint/lint.py (+77) and a new lint/secret_scan.py (984 lines). The patch file itself is already deleted. Read all of it critically first: keep what is right, fix or rewrite what isn't, and finish what is missing. Nothing in it is trusted because it exists.

1. docs/design.md FIRST: make "### 6.7 Secret lint rules (L011, L014)" complete and true to the final code in the doc's style (tables, short bullets), placed between §6.2 and §6.8 (the index row already exists). Contents: what each rule catches (sources, sinks, taint through names, magics), exemptions, severity and config keys, message and fix text, how it relates to §6.8 redaction (L011 stops the print before it runs; redaction is the net for what still reaches Claude), known gaps (e.g. cross-cell taint if not covered, obfuscation such as getattr/exec, secrets read from files), and the first-cell.md check.

2. L011 secret_print, a configurable hard rule, exactly like L008–L010 (lint/lint.py _CHECKS "configurable hard rules first"; defaults.toml [lint.rules] \`secret_print = "error"\`). A cell is refused when it DISPLAYS an env-sourced value:
   - Sources: os.environ[...] / os.environ.get(...) / os.getenv(...) / os.environ itself / dict(os.environ) / os.environ.copy() / dotenv_values(...) (also dotenv.dotenv_values, and the same names imported with from-imports such as \`from os import environ, getenv\`), plus names assigned from these in the cell (simple dataflow: assignment, augmented/unpacking where easy, f-strings and concatenation built from a tainted name stay tainted).
   - Sinks: the cell's last expression (Jupyter displays it), print(...), display(...), repr(...)/str(...) when themselves displayed, pprint/pp, an f-string or %/format string inside a print. Choose sensible extra sinks (sys.stdout.write, logging calls) if cheap and justify them in the design doc.
   - Magics (use the existing lint/magics.py masking to find them): \`%env\` without \`=\` (lists all, or shows one), \`!env\`, \`!printenv\`, \`!set\`, \`!export -p\`, \`!echo $X\` (a shell line that expands a $VAR or \${VAR} into output).
   - Exempt (never flagged): len(x), bool(x), \`x is None\`/\`x is not None\`, \`"X" in os.environ\`, os.environ.keys() / list(os.environ) (names only), comparisons that yield a bool, and passing the value to a non-display call (e.g. create_engine(url), login(token=tok)).
   - Message: one line naming what would be printed (the env var name or the variable, never a value); fix line suggesting a check that doesn't print it, e.g. \`print("OPENAI_API_KEY" in os.environ)\` or \`print(bool(os.getenv("OPENAI_API_KEY")))\`. Keep the style of the other lint messages (_error / _quote).
   - plugins/nh/skills/init/reference/first-cell.md: its code must lint clean with L011 on (add a test that lints the code blocks in that file through the real lint_cell).

3. L014 secret_name, a hint (L1xx-style placement in the hints list, near the top because it matters; defaults.toml [lint.rules] \`secret_name = "hint"\`). Flags DISPLAYING an identifier whose name is secret-shaped per C3's \`secrets.is_secret_name\` (reuse it; do not re-implement name matching): the same sinks as L011, for bare names and attribute names (e.g. print(api_key), cfg.db_password as the last expression, f"{token_value}" in print... whatever is_secret_name says). "tokens", "tokenizer", "author", "max_tokens", SORT_KEY must not fire (is_secret_name already handles them). Don't report L014 for a name that L011 already reports in the same cell. Strict mode turns it into an error, as for every hint (document it).

4. Config and docs: defaults.toml keys with comments like their neighbours; docs/harness-toml.md rows for both keys (tests/unit/test_skill_files.py ~L625-648 pins documented keys vs defaults); plugins/nh/skills/notebook/reference/errors.md rows for L011 and L014 next to L008–L010 and the hints; any other doc that lists the hard rules (graphify/grep for L010 and markdown_output). Check whether plan item "C5 moves package_install to ask" affects anything here — it does not; do not change L009.

5. Eval case: plugins/nh/evals/secret-print-refused/, modelled on the existing cases (read plugins/nh/evals/explain-only, one-cell-per-turn, note-shape, _scaffold/base.sh and mocks/ to learn the format, and the eval CLI docs at ${DOCS}). Scenario: the user asks nh to check that their API key is loaded (e.g. "check that OPENAI_API_KEY is set"); graders: no cell ever prints or displays the value (grade the calls themselves with target: mock_calls, or an expect guard, whichever the docs support best, proven with Python re against good and bad code), the check uses a non-printing form, and the reply says whether the key is set without showing it. Keep every regex inside the eval CLI's small dialect (no groups, few quantifiers; see how the existing expect guards are written). If the case uses fixed mocks, add them to MOCK_SCENARIOS in tests/unit/test_skill_files.py (add new entries at the END of the dict, after the slash-explain entry) so the drift test compares them with the real gateway. Then RUN it from a clean copy (see the eval rules above), --case 'secret-print-refused', and iterate by evidence until two consecutive rounds of 3 runs each score 1.0 on every run with none aborted (6/6), at most 6 rounds; fix causes (mock inconsistency, format mismatch, unclear prompt), never weaken a grader to pass. \`claude plugin validate --strict plugins/nh\` must still pass and any test that enumerates eval cases stays green.

6. Tests: tests/unit/test_lint_hard.py (L011: every source, sink, magic, taint through names and f-strings, every exemption, a multi-line cell, the configurable levels off/hint/error, message has no value, first-cell.md clean); tests/unit/test_lint_hints.py (L014 fires on secret-shaped names and not on the must-not-match list, not duplicated with L011, strict mode); a gateway test that nh_add_cell with a secret-printing cell is refused with L011 and counts as a lint reject, and the writer path too if lint differs there; update any test that pins the rule list or defaults. Never weaken an existing assertion.

7. Gates you run before returning: targeted files, then the FULL unit suite once, Gc (the py3.11 command), ruff check, ruff format --check, pyright src, plugin validate (both), graphify update .
`

const IMPL_SCHEMA = {
  type: 'object',
  properties: {
    summary: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    rules: { type: 'array', items: { type: 'string' }, description: 'each source / sink / magic / exemption -> test name' },
    tests_run: { type: 'string', description: 'commands and pass/fail counts' },
    deviations: { type: 'array', items: { type: 'string' }, description: 'anything not done exactly as specified, with reason' },
  },
  required: ['summary', 'files_changed', 'rules', 'tests_run', 'deviations'],
}

const FINDINGS_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          file: { type: 'string' },
          line: { type: 'integer' },
          issue: { type: 'string' },
          evidence: { type: 'string', description: 'what you read or ran that shows it' },
          fix: { type: 'string' },
        },
        required: ['severity', 'file', 'issue', 'evidence', 'fix'],
      },
    },
    checked: { type: 'string', description: 'what you verified and found correct' },
  },
  required: ['findings', 'checked'],
}

phase('Implement')
log('C4 implement: check and finish the partial work, design §6.7 first, then L011, L014, config and docs, the eval case (run until 6/6), tests')
const impl = await agent(
  `You implement chunk C4 of the nh v0.2 build.\n${RULES}\n${SCOPE}\nYour key: impl. Work in order: read the partial work, design doc, L011, L014, config and docs, tests, eval case (run it), graphify update, then the gates in item 7. Return the structured summary; list every deviation honestly.`,
  { label: 'implement:C4', phase: 'Implement', schema: IMPL_SCHEMA },
)
if (!impl) return { error: 'implement agent returned nothing' }
log(`Implement done: ${impl.files_changed.length} files; deviations: ${impl.deviations.length}`)

phase('Review')
const LENSES = [
  { key: 'plan', prompt: `Lens: correctness against the plan and design. Check every C4 scope item against the code: L011's sources, sinks, taint, magics and exemptions; it is a configurable hard rule placed and configured like L008–L010 and counts as a lint reject; L014 reuses secrets.is_secret_name, sits with the hints, is not duplicated with L011, and strict mode makes it an error; defaults.toml, harness-toml.md, errors.md and design §6.7 match the code exactly; first-cell.md lints clean; the eval case follows the existing format; nothing outside C4's scope changed (git -C ${R} diff). Also judge the eval case: does it test what it claims, is the prompt natural, would a plugin that prints the key fail it, and are its regexes inside the CLI dialect (prove with Python re on good and bad code)? Run it once (one round of 3) from your own clean copy with --keep-temp and read the transcripts.` },
  { key: 'bypass', prompt: `Lens: adversarial bypasses and false positives. Write small scripts under a scratch dir in ${S}/review-bypass (not in the repo) that call the real lint_cell with the default config. Try to print an env value past L011: aliases (import os as o; from os import environ as E; getenv via from-import), os.environ.get with a default, chained names (a = os.environ; b = a["X"]; print(b)), f-strings, "%s" % x, "{}".format(x), str concatenation, print(*[x]), display(x), a tuple/dict as the last expression holding the value, x.strip(), x[:4], printing inside a for loop over os.environ.items(), json.dumps(dict(os.environ)), pprint, dotenv_values(".env") displayed, !echo $TOKEN, %env, %env VAR, !printenv VAR, a magic inside a multi-line cell. Then false positives: len/bool/is None/in os.environ checks, os.environ.keys(), list(os.environ), passing the value to a function that doesn't display it, assigning without displaying, %env VAR=value (setting), printing an unrelated variable after an env read, a DataFrame named tokens, print(tokenizer), print(author), max_tokens, SORT_KEY, and the code in plugins/nh/skills/init/reference/first-cell.md and the other skill reference examples (lint them all: nh's own examples must never trip L011). Report each bypass or false positive with the exact input and output; judge which ones matter for a model that follows instructions (the gate is not an adversarial boundary) and rate severity accordingly.` },
  { key: 'tests-pins', prompt: `Lens: test coverage, pins and gates. Map every new branch in the C4 delta (\`git -C ${R} diff\` plus untracked files) to a test that would fail without it; list untested branches and weak assertions. Check the pins stay intact: INSTRUCTIONS ≤ 2048 chars and byte-identical to HEAD, RULE/QA_REPORT/NO_PROMPT_ID byte-identical, SKILL.md ≤ 150 lines, 5 tools, test_skill_files' documented-keys check, any rule-list pin. Run the changed/added test files, the Gc py3.11 command from ci.yml, then the FULL unit suite once, ruff check/format --check, pyright src, and claude plugin validate --strict plugins/nh and .; report counts.` },
]
const reviews = await parallel(LENSES.map(l => () => agent(
  `You review chunk C4 of the nh v0.2 build (uncommitted, unstaged changes in the main tree ${R}; the C4 delta is \`git -C ${R} diff\` plus untracked files). Do NOT edit any file in the repo. Your key: review-${l.key}.\n${RULES}\n${SCOPE}\nImplementer's report: ${JSON.stringify(impl)}\n\n${l.prompt}\nReport only findings you verified by reading code or running something; put the evidence in each finding.`,
  { label: `review:${l.key}`, phase: 'Review', schema: FINDINGS_SCHEMA },
)))
const findings = reviews.filter(Boolean).flatMap((r, i) => r.findings.map(f => ({ ...f, lens: LENSES[i].key })))
log(`Review done: ${findings.length} findings (${findings.filter(f => f.severity !== 'minor').length} blocker/major)`)

phase('Fix')
const fix = await agent(
  `You fix chunk C4 of the nh v0.2 build after review.\n${RULES}\n${SCOPE}\nReview findings (verify each against the code first; fix every real one, including minors; if you reject one, say why): ${JSON.stringify(findings)}\n\nYour key: fix. After fixing: graphify update . (from ${R}), rerun the affected test files, the Gc py3.11 command, then the FULL unit suite once, ruff check/format --check, pyright src, claude plugin validate --strict plugins/nh and .; then rerun the secret-print-refused case from a fresh clean copy until two consecutive rounds of 3 runs all score 1.0 with none aborted (at most 4 rounds), and report the rounds in tests_run. Return the structured summary (deviations = findings you rejected, with reasons).`,
  { label: 'fix:C4', phase: 'Fix', schema: IMPL_SCHEMA },
)
return { impl, findings, reviewsChecked: reviews.filter(Boolean).map(r => r.checked), fix }
