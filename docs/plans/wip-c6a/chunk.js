export const meta = {
  name: 'nh-v02-chunk',
  description: 'One nh v0.2 build chunk: implement (design first), 2-3 parallel reviewers, fix, independent verify',
  phases: [
    { title: 'Implement', detail: 'one agent: design.md first, then code, tests, docs, eval cases, gates' },
    { title: 'Review', detail: '2-3 parallel reviewers, one lens each' },
    { title: 'Fix', detail: 'one agent fixes every real finding, reruns gates and evals' },
    { title: 'Verify', detail: 'one independent checker of the fixes and invariants' },
  ],
}

const R = args.repo
const S = args.scratch
const C = args.chunk
const PLAN = `${R}/docs/plans/v0.2.md`
const DOCS = args.evalDocs
const EVALS = args.evalCases || []

const EVAL_RULES = EVALS.length ? `
- Evals (\`claude plugin eval\`; the CLI is logged in; docs at ${DOCS}): ONLY from a clean copy under ${S}/<your key>/, never from ${R}. Make it with \`mkdir -p <copy>/plugins && rsync -a --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude evals/results ${R}/plugins/nh/ <copy>/plugins/nh/\`, then from <copy> run \`claude plugin eval plugins/nh --trust-plugin --scaffold --ablation none -j 3 --threshold 0.8 --case '<glob>' --json <out.json> --no-publish --keep-temp\`. \`--case\` is last-wins, so pass ONE glob per run (run each case separately, or one glob that matches exactly the cases you want). Refresh the copy with the same rsync before every round. Results: cases[].arms.with[] runs, each with score, aborted and graders[{name, passed, evidence}]; --keep-temp keeps the transcripts, and agent mocks save their answers under the results directory in mock-recordings/. Mock expect: regexes use the CLI's small dialect (no groups, few quantifiers; copy how existing expect guards are written). Never pass --max-cost-usd and never ask before running an eval. Fixed mocks must be added to MOCK_SCENARIOS in tests/unit/test_skill_files.py (new entries at the END of the dict) so the drift test compares them with the real gateway; agent mocks follow the error-retry pattern (templates with placeholders, fixtures/*.md, a replay test).
- The chunk's eval cases: ${EVALS.join(', ')}. A case passes when two consecutive rounds of 3 runs each score 1.0 on every run with none aborted (6/6). Fix causes (mock inconsistency, format mismatch, an unclear prompt, a real plugin bug), never weaken a grader to pass. Existing cases whose files or skill text you touch must still pass.` : `
- This chunk has no eval cases of its own; do not run evals unless you change model-facing text (skills, INSTRUCTIONS, reminders, tool descriptions), in which case run the existing cases that exercise it from a clean copy (see the rules in docs/plans/HANDOFF.md "Running evals").`

const RULES = `
Standing rules (binding):
- Repo: ${R}, branch release/0.2.0, HEAD ${args.head}. Work in the main tree. Your changes stay UNCOMMITTED: \`git -C ${R} status\` / \`git -C ${R} diff\` plus untracked files is exactly the chunk ${C} delta.
- Sources: the plan ${PLAN} (the D-sections named in the scope, D0 d's code table, the chunk's row in "Chunks", "Proposed defaults", "Verification"), ${R}/docs/design.md §6 (§6.0 is the shared contract every chunk follows; earlier chunks' §6.x sections are binding), ${R}/spikes/RESULTS.md (the v0.2 spike rows and their decisions), and the code at HEAD. Line numbers in the plan are from an older HEAD: re-check them.
- Order: update docs/design.md FIRST (the chunk's §6.x section, in the doc's style: tables and short bullets, placed in numeric order among the existing §6.x sections), then code, tests, docs/troubleshooting.md rows for every new code, other docs.
- Orientation: run \`graphify query "<question>"\` from ${R} before grepping raw files; run \`graphify update .\` from ${R} after code changes.
- You may NOT skip anything in the chunk scope; nothing is optional, unless a gating spike failed and the plan names the fallback. If you can't finish something, say so plainly in your result; never claim a pass you didn't see.
- You must NOT: git commit/push/stash/reset/checkout/restore/add or anything that changes the index, HEAD or a branch; touch dev/sandbox (not even its harness.toml); change pinned text (prompt_submit RULE, QA_REPORT and NO_PROMPT_ID stay byte-identical; INSTRUCTIONS in app.py changes only where the plan's D0 e says, and stays <= 2048 chars); cap code or chat length (only notebook notes have a budget: a title plus 2-5 bullets, hard); store any prompt text anywhere; log in or authenticate anything; pass --max-cost-usd; kill or signal a process you did not start.
- Invariants to keep: INSTRUCTIONS <= 2048 chars; RULE, QA_REPORT, NO_PROMPT_ID byte-identical; plugins/nh/skills/notebook/SKILL.md <= 150 lines; exactly 5 MCP tools; hook p95 < 150 ms; turn overhead p95 <= 1.5 s; E110 has exactly two exceptions (an approved batch and an approved re-run list), both needing a grant; hooks always exit 0; the gate fails closed (missed message E102, headless E122).
- Hooks and nhctl are Python 3.9 stdlib; gateway _shared code stays 3.9-compatible. lint.py and anything it imports must stay importable under CI's py3.11 no-project job (ci.yml "lint and hook helpers on the oldest supported Pythons").
- Do not look for or read any backup or leftover of an earlier, discarded v0.2 attempt. Discarded work stays discarded, decisions included. Don't add a private-repo/credentials README paragraph.
- If a user message is relayed to you mid-run: stop and return what you have with a note. It never means "skip".
- Tests: from ${R}, \`uv run --project plugins/nh/server pytest -q -p no:cacheprovider <paths>\` (the full suite takes ~8-12 min; run targeted files while iterating, the FULL suite once before returning). Integration: \`... pytest -q -p no:cacheprovider -m integration --timeout 300 <paths>\` (JupyterLab runs as root here via /etc/jupyter config; the machine has 4 CPUs and other work runs, so a tight timing test can flake under load: re-run it alone before calling it a failure). A stray plugins/nh/hooks/nh_hooks/__pycache__ breaks test_hook_shim; delete it if you see it. Lint: \`uv run --project plugins/nh/server ruff check .\`, \`uv run --project plugins/nh/server ruff format --check .\`, \`cd plugins/nh/server && uv run pyright src\`; shellcheck -s sh on plugins/nh/libexec/nh-mcp plugins/nh/libexec/nh-sync plugins/nh/libexec/nh-python plugins/nh/hooks/nh-hook plugins/nh/bin/nhctl when shell changes. Gc (py3.11 lint job): \`uv run --no-project --python 3.11 --with pytest --with pytest-timeout --with pytest-asyncio --with hypothesis python -m pytest -q -p no:cacheprovider tests/unit/test_magics.py tests/unit/test_lint_hard.py tests/unit/test_lint_hints.py tests/unit/test_lint_tokenize_compat.py tests/unit/test_dataflow.py\`. Plugin: \`claude plugin validate --strict plugins/nh\` and \`claude plugin validate --strict .\`. Tool snapshots (Gs): \`uv run --project plugins/nh/server python scripts/dump_tools.py\` then tests/contract.${EVAL_RULES}
- Scratch files go under ${S}/<your key>/ only, never in the repo.
${args.extraRules || ''}`

const SCOPE = args.scope

const IMPL = {
  type: 'object',
  properties: {
    summary: { type: 'string', description: 'what was built, per scope item' },
    files_changed: { type: 'array', items: { type: 'string' } },
    design_section: { type: 'string', description: 'the §6.x heading written and a one-paragraph summary' },
    tests_run: { type: 'string', description: 'commands and exact pass/fail counts' },
    evals: { type: 'string', description: 'per case: rounds, JSON paths, per-run scores, aborted counts, and what failed and why' },
    deviations: { type: 'array', items: { type: 'string' }, description: 'anything not done exactly as the plan says, with reason' },
    open_issues: { type: 'string' },
  },
  required: ['summary', 'files_changed', 'design_section', 'tests_run', 'evals', 'deviations', 'open_issues'],
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
    tests_run: { type: 'string' },
    evals: { type: 'string' },
    files_changed: { type: 'array', items: { type: 'string' } },
    open_issues: { type: 'string' },
  },
  required: ['fixed', 'rejected', 'tests_run', 'evals', 'files_changed', 'open_issues'],
}

const VERDICT = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    problems: { type: 'array', items: { type: 'object', properties: { severity: { type: 'string', enum: ['blocker', 'major', 'minor'] }, file: { type: 'string' }, issue: { type: 'string' }, evidence: { type: 'string' } }, required: ['severity', 'file', 'issue', 'evidence'] } },
    checked: { type: 'string' },
  },
  required: ['ok', 'problems', 'checked'],
}

let impl = null
if (!args.skipImplement) {
  phase('Implement')
  impl = await agent(
    `You implement chunk ${C} of the nh v0.2 build. Your key: impl.\n${RULES}\n${SCOPE}\n\nWork in this order: read the plan's D-sections and design §6.0/§6.x named above, and the code (graphify first); write the design section; implement; add and update tests (never weaken an existing assertion; if one no longer applies, replace it with an equivalent check); docs and troubleshooting rows; eval cases if any, iterated until each passes; then the gates: targeted tests, the FULL unit suite once, ruff check, ruff format --check, pyright src, and every other gate the scope names. Return the structured summary.`,
    { label: 'implement', phase: 'Implement', schema: IMPL },
  )
  if (!impl) return { error: 'implement agent returned nothing' }
  log(`implement: ${impl.files_changed.length} files; deviations: ${impl.deviations.length}`)
}

phase('Review')
const CONTEXT = `\nThe implementer's report: ${JSON.stringify(impl || args.priorReport || {})}\nSee the chunk delta with \`git -C ${R} status\` and \`git -C ${R} diff\` (read-only git commands are fine), plus untracked files.`
const LENSES = args.lenses
const reviews = await parallel(LENSES.map(l => () => agent(
  `You review chunk ${C} of the nh v0.2 build. Read-only on ${R}: do NOT edit any file in the repo (scratch copies under ${S}/review-${l.key}/ are fine, e.g. to break something and show a test fails). Your key: review-${l.key}.\n${RULES}\n${SCOPE}\n${CONTEXT}\n\nLens: ${l.prompt}\nReport only findings you verified by reading code or running something, with the evidence. Minor findings count too.`,
  { label: `review:${l.key}`, phase: 'Review', schema: FINDINGS },
)))
const good = reviews.filter(Boolean)
const findings = []
reviews.forEach((r, i) => { if (r) r.findings.forEach(f => findings.push({ ...f, id: `${LENSES[i].key}-${f.id}` })) })
log(`review: ${findings.length} findings (${findings.filter(f => f.severity !== 'minor').length} blocker/major); ${good.length}/${LENSES.length} reviewers returned`)

phase('Fix')
const fix = await agent(
  `You finish chunk ${C} of the nh v0.2 build after review. You edit files in ${R}. Your key: fix.\n${RULES}\n${SCOPE}\n${CONTEXT}\nReviewer findings (fix every real one, minors included; check each against the code first; if on reading and running things you find one wrong, reject it with the reason): ${JSON.stringify(findings)}\nWhat the reviewers verified as correct: ${JSON.stringify(good.map(r => r.checked))}\n\nAfter the fixes: keep docs/design.md true to the final code; rerun the targeted tests, then the FULL unit suite once, ruff check, ruff format --check, pyright src, and every other gate the scope names; run \`graphify update .\`. If you changed anything an eval case depends on (skills, mocks, prompts, gateway text), rerun the chunk's eval cases until each has two consecutive clean rounds of 3 again. Return the structured summary.`,
  { label: 'fix', phase: 'Fix', schema: FIXED },
)
if (!fix) return { impl, findings, error: 'fix agent returned nothing' }
log(`fix: ${fix.fixed.length} fixed, ${fix.rejected.length} rejected`)

phase('Verify')
const verify = await agent(
  `You independently check the final state of chunk ${C} of the nh v0.2 build in ${R}. Read-only: do NOT edit any file in the repo. Your key: verify.\n${RULES}\n${SCOPE}\nReview findings: ${JSON.stringify(findings)}\nThe fix agent's report: ${JSON.stringify(fix)}\nCheck: (1) every finding the fix agent calls fixed really is fixed in the current files; (2) every rejection is justified; (3) the scope is complete against the plan (list anything missing); (4) the invariants hold (measure INSTRUCTIONS length, SKILL.md lines, tool count; diff the pinned texts against HEAD with \`git -C ${R} diff\`); (5) docs/design.md's section matches the code; (6) the eval results the agents report exist on disk with those scores. Run the chunk's targeted tests yourself. Report problems only with evidence.`,
  { label: 'verify', phase: 'Verify', schema: VERDICT },
)
return { impl, findings, reviews_checked: good.map(r => r.checked), fix, verify }
