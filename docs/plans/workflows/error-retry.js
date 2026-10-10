export const meta = {
  name: 'nh-v02-error-retry-eval',
  description: 'Rework the error-retry eval case so it passes reliably without weakening it: agent mocks that simulate the sent code, a prompt that makes the failure natural, drift tests kept',
  phases: [
    { title: 'Implement', detail: 'one agent reworks the case in a worktree and iterates until 2 consecutive rounds of 3 runs all pass' },
    { title: 'Review', detail: 'two reviewers: validity and anti-gaming; tests, formats and the other cases' },
    { title: 'Fix', detail: 'one agent fixes confirmed findings and reruns two rounds' },
  ],
}

const R = args.repo
const W = args.wt
const S = args.scratch
const DOCS = args.evalDocs
const EV = args.evidence

const RULES = `
Standing rules (binding):
- Work tree: ${W} is a detached git worktree of ${R} at HEAD ${args.head} (branch release/0.2.0 of the main tree). Edit ONLY files under ${W}. The main tree ${R} is read-only for you: another build chunk is being worked on there, so never edit, stage or run tests in ${R}.
- Orientation: \`graphify query "<question>"\` runs from ${R} (graphify-out/ is gitignored, so it isn't in the worktree). Run it before grepping raw files.
- You may NOT skip anything in your scope. Nothing is optional. If you can't finish something, say so plainly in your result; never claim a pass you didn't see.
- You must NOT: git commit/push/stash/reset/checkout/add/worktree or anything that changes an index, a HEAD or a branch; touch dev/sandbox (not even its harness.toml); change the gateway's or hooks' source, the pinned texts or any other eval case (this task is only plugins/nh/evals/error-retry/ and the tests about it; a shared suite mock may change only if the case truly can't work otherwise, and then every case using it must still pass); weaken a grader (the three graders keep their meaning: exactly one nh_add_cell, one or two nh_edit_cell, a reply that says what failed and why, what changed, and the monthly counts); cap code or chat length or pass --max-cost-usd; store prompt text anywhere; log in or authenticate anything; kill or signal a process you did not start (never the desktop app's claude processes, never the user's jupyter-lab).
- Do not look for or read any backup or leftover of an earlier, discarded v0.2 attempt.
- If a user message is relayed to you mid-run: stop and return what you have with a note. It never means "skip".
- Tests: from ${W}, \`uv run --project plugins/nh/server pytest -q -p no:cacheprovider <paths>\` (the first run creates the worktree's venv; that's expected).
- Evals (\`claude plugin eval\`; the CLI is logged in): ONLY from a clean copy, never from ${W} or ${R}. Make it with \`mkdir -p <copy>/plugins && rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude evals/results ${W}/plugins/nh/ <copy>/plugins/nh/\`, then from <copy> run \`claude plugin eval plugins/nh --trust-plugin --scaffold --ablation none -j 3 --threshold 0.8 --case 'error-retry' --json <out.json> --no-publish --keep-temp\`. Refresh the copy with the same rsync before every round. The result JSON has cases[].arms.with[] runs, each with score, aborted and graders[{name, passed, evidence}]. --keep-temp keeps each run's transcript, and a clean run saves agent-mock answers under the results directory in mock-recordings/ (with ADOPT.txt): read them to see exactly what the model sent and what the mocks answered.
- Scratch files go under ${S}/<your key>/ only, never in the repo.
`

const TASK = `
The case: plugins/nh/evals/error-retry/ (case.yaml, prompt.md, scaffold.sh sourcing ../_scaffold/base.sh, graders/explains-fix.md llm, graders/one-add-cell.md tool_used nh_add_cell 1..1, graders/retries-in-place.md tool_used nh_edit_cell 1..2, and fixed case mocks mocks/nh/nh_add_cell.md and nh_edit_cell.md). Suite mocks in plugins/nh/evals/mocks/nh/ (nh_add_cell, nh_edit_cell, nh_inspect, nh_run, nh_undo, _tools.json); the shared nh_inspect.md is a fixed overview of the fixture notebook (cell [1] only). The fixture (base.sh): data/sales.csv, 43 rows, row 19 (order_id 1020) has order_date 2024-02-30, and 6 prices are missing; the notebook has the loader cell [1] "Load raw data and check schema" (id nh-3b8f2a61c0) that makes df.

What it should test: a cell that fails is fixed in place with nh_edit_cell (one or two retries), never with a second nh_add_cell, and the reply says honestly what failed and why, what was changed, and the monthly counts.

Why it fails today, at HEAD and at v0.1.1 alike (so it's the case, not the plugin): the model's first cell now parses defensively with errors="coerce", but the fixed add mock still answers with a ValueError from a hardcoded line of code the model never wrote, and a fixed "[2]"; the fixed edit mock answers for "Count orders per month" [3] with a fixed title; the shared inspect overview doesn't show the new cell. The model notices these contradictions ("the code that ran isn't the code I sent", "the notebook doesn't show the change") and stops without retrying, or reports distrust. Evidence: the saved runs ${EV} (evals*.json and evals-er*.json; read error-retry's graders' evidence). One earlier attempt, set aside for reference only, is at ${S}/../c3check/er-wip (tracked.patch: a prompt and add-mock echo change; nh_inspect.md: an agent inspect mock). Its lesson: the small mock model copied the example content in the instructions as if it were the notebook's state, and rejected the status view. So agent-mock instructions must show nh's formats as templates with placeholders only (no example values that look like state), must describe every nh_inspect view (overview, cell, outline, status, vars, and whatever else the real tool has; check tools.md and the gateway), and must derive everything from the calls actually made and from the fixture facts.

Direction (the user approved this):
- Prompt: keep it a natural user request, but make the failure natural and the end state clear. For example: ask to parse order_date strictly (no errors="coerce"), so a bad date raises, and if a date can't be parsed, to leave that order out of the counts and say which one. Don't tell the model which tool to use or that it will fail. Tune the wording by evidence, but it must stay something a real user could type.
- Mocks: case-level \`type: agent\` mocks for nh_add_cell (keep its expect guard: title /^[^\\n]{1,80}$/, intent /\\S/, code string), nh_edit_cell and nh_inspect (and nh_run/nh_undo if the model may call them), so the answers follow the code actually sent: simulate pandas on the fixture facts (strict to_datetime on 2024-02-30 raises "ValueError: day is out of range for month" with nh's error format and the failing line from the sent code; a fixed version prints what that code prints, with the real counts: 2024-01 8, 2024-02 7, 2024-03 6, 2024-04 7, 2024-05 7, 2024-06 7 once order 1020 is left out; verify these with pandas on the fixture yourself). Cell numbers, titles and ids must be consistent across calls (the added cell is [2] below [1]; an edit keeps its title and number; retries count 1 of 2, 2 of 2; after two failed retries nh refuses a third). The formats must match what the real gateway returns: get them from \`run_mock_scenario\` in tests/unit/test_skill_files.py (the real gateway with FakeBackend and the real hooks) and from the gateway source; show them as templates. Find out whether separate per-tool agent mocks share one history (does nh_inspect's agent see the add and edit calls?) from the recordings/transcripts; if they don't, use \`_server.md\` with tools: listing them, noting that a <tool>.md takes precedence over _server.md and that the case's files override the suite's file by file (so a suite <tool>.md would still win over the case's _server.md unless the case overrides that file too); keep the expect guard on nh_add_cell.md as the docs require. Use abort_when only for real misuse, if at all.
- Tests: update tests/unit/test_skill_files.py. MOCK_SCENARIOS, run_mock_scenario, result_shape and test_mock_matches_the_real_gateway_result check each fixed mock body's shape against the real gateway; keep drift protection for the agent mocks too (e.g. the templates' machine line, section headers and the Next wording must match what the real gateway prints for the equivalent scenario), and keep MOCK_KEYS, the expect-guard test and every other test meaningful. Never weaken an existing assertion; if one no longer applies to a changed file, replace it with an equivalent check.
- Update any doc that describes this case (grep plugins/nh/evals and docs for error-retry).
`

const IMPL = {
  type: 'object',
  properties: {
    changed_files: { type: 'array', items: { type: 'string' } },
    design: { type: 'string', description: 'what the case now does and why; how the mocks keep state; whether per-tool agent mocks share history (with the evidence)' },
    rounds: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          round: { type: 'integer' },
          change: { type: 'string', description: 'what you changed before this round' },
          json: { type: 'string', description: 'path of the result JSON' },
          scores: { type: 'array', items: { type: 'number' } },
          aborted: { type: 'integer' },
          failures: { type: 'string', description: 'which graders failed and why, from the evidence and transcripts' },
        },
        required: ['round', 'change', 'json', 'scores', 'aborted', 'failures'],
      },
    },
    consecutive_clean_rounds: { type: 'integer', description: 'rounds of 3 with every run scoring 1.0, counted at the end' },
    tests: { type: 'string', description: 'test commands run and their exact results' },
    open_issues: { type: 'string' },
  },
  required: ['changed_files', 'design', 'rounds', 'consecutive_clean_rounds', 'tests', 'open_issues'],
}

const FINDINGS = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          file: { type: 'string' },
          issue: { type: 'string' },
          evidence: { type: 'string', description: 'what you read or ran that shows it, with exact inputs and outputs' },
          fix: { type: 'string' },
        },
        required: ['id', 'severity', 'file', 'issue', 'evidence', 'fix'],
      },
    },
    checked: { type: 'string', description: 'what you verified and found correct' },
  },
  required: ['findings', 'checked'],
}

const FIXED = {
  type: 'object',
  properties: {
    fixed: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, change: { type: 'string' } }, required: ['id', 'change'] } },
    rejected: { type: 'array', items: { type: 'object', properties: { id: { type: 'string' }, reason: { type: 'string' } }, required: ['id', 'reason'] } },
    rounds: IMPL.properties.rounds,
    consecutive_clean_rounds: { type: 'integer' },
    tests: { type: 'string' },
    changed_files: { type: 'array', items: { type: 'string' } },
    open_issues: { type: 'string' },
  },
  required: ['fixed', 'rejected', 'rounds', 'consecutive_clean_rounds', 'tests', 'changed_files', 'open_issues'],
}

phase('Implement')
const impl = await agent(
  `You rework the error-retry eval case of the nh plugin so it passes reliably while still testing what it claims. Your key: impl.\n${RULES}\n${TASK}\n\nSteps: read the eval CLI docs at ${DOCS} (case.yaml, prompt.md, graders, mocks: fixed, agent, _server.md, expect, substitutions, replay, results); read the evidence; read the case, the suite mocks, tests/unit/test_skill_files.py (MOCK_SCENARIOS and the mock tests) and the gateway's result formats (run_mock_scenario on the error-retry scenarios; the real add, edit, inspect and error texts). Then rework the case and the tests. Iterate by evidence: after each round read the failing runs' transcripts and the mock recordings, and fix the cause (a mock inconsistency, a format mismatch, an unclear prompt), never the grader. Stop when two consecutive rounds of 3 runs each score 1.0 on every run with none aborted (6/6), or after 8 rounds. Before returning, run tests/unit/test_skill_files.py and the full tests/unit directory, \`uv run --project plugins/nh/server ruff check .\` and \`ruff format --check .\` from ${W}, and \`claude plugin validate --strict plugins/nh\` from ${W}. Return the structured summary.`,
  { label: 'implement', phase: 'Implement', schema: IMPL },
)
if (!impl) return { error: 'implement agent returned nothing' }
log(`implement: ${impl.consecutive_clean_rounds} consecutive clean rounds over ${impl.rounds.length} rounds; ${impl.changed_files.length} files`)

phase('Review')
const CONTEXT = `\nThe implement agent's summary: ${JSON.stringify(impl)}\nSee its changes with \`git -C ${W} status\` and \`git -C ${W} diff\` (read-only git commands are fine), plus untracked files.`
const LENSES = [
  {
    key: 'validity',
    prompt: `Lens: validity and anti-gaming. Does the case still test what it claims: a failing cell fixed in place with nh_edit_cell (one or two retries, never a second nh_add_cell) and an honest reply (what failed and why, what changed, the counts)? Is the prompt something a real user could type, without naming tools or giving away the failure or the fix? Do the agent mocks simulate faithfully (answers follow the sent code and the fixture; no extra steering beyond what the real gateway's Next says; no answer that only fits the expected path)? Would a broken plugin still pass: one that adds a second cell, hides the failure, retries three times, or never retries? Are the graders' meanings intact (compare with \`git -C ${W} diff\`)? Run one round of 3 yourself from your own clean copy with --keep-temp, and read every run's transcript and the mock recordings to confirm the mocks behaved as designed.`,
  },
  {
    key: 'tests',
    prompt: `Lens: tests, formats and the rest of the suite. Is drift protection for the new mocks real (would a change to the gateway's add, edit, error or inspect format make a test fail)? Prove it by temporarily breaking a format in a scratch copy of the relevant source or template, never in ${W}. Are MOCK_SCENARIOS, EXACT_MOCKS, MOCK_KEYS and the expect-guard test still meaningful, and no existing assertion weakened? Do the templates match what run_mock_scenario prints for the equivalent calls? Are the other cases unaffected (no shared suite mock changed, or if one was, is every case using it still correct)? Are the docs that describe this case updated? Run tests/unit/test_skill_files.py, the full tests/unit directory, ruff check and ruff format --check from ${W}, and \`claude plugin validate --strict plugins/nh\` from ${W}.`,
  },
]
const reviews = await parallel(LENSES.map(l => () => agent(
  `You review a rework of the nh plugin's error-retry eval case. Read-only on ${W} (and on ${R}). Your key: review-${l.key}.\n${RULES}\n${TASK}\n${CONTEXT}\n${l.prompt}\nReport only findings you verified by reading or running something, with the evidence. Minor findings count too.`,
  { label: `review:${l.key}`, phase: 'Review', schema: FINDINGS },
)))
const findings = reviews.filter(Boolean).flatMap((r, i) => r.findings.map(f => ({ ...f, id: `${LENSES[i].key}-${f.id}` })))
log(`review: ${findings.length} findings (${findings.filter(f => f.severity !== 'minor').length} blocker/major)`)

phase('Fix')
const fix = await agent(
  `You finish the rework of the nh plugin's error-retry eval case after review. Your key: fix.\n${RULES}\n${TASK}\n${CONTEXT}\nReviewer findings (fix every one, minors included; if on reading and running things you find one wrong, reject it with the reason): ${JSON.stringify(findings)}\nWhat the reviewers verified as correct: ${JSON.stringify(reviews.filter(Boolean).map(r => r.checked))}\n\nAfter the fixes, rerun the case from a fresh clean copy until two consecutive rounds of 3 runs each score 1.0 on every run with none aborted (6/6), at most 6 rounds; if a round fails, find the cause in the transcripts and fix that, never a grader. Then run tests/unit/test_skill_files.py, the full tests/unit directory, ruff check and ruff format --check from ${W}, and \`claude plugin validate --strict plugins/nh\` from ${W}. Return the structured summary.`,
  { label: 'fix', phase: 'Fix', schema: FIXED },
)
return { impl, findings, reviews_checked: reviews.filter(Boolean).map(r => r.checked), fix }
