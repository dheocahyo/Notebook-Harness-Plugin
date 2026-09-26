export const meta = {
  name: 'qa-cell',
  description: "Write this message's one notebook cell with nh:cell-writer, then live-check it with nh:cell-qa",
  whenToUse: 'nh projects only: when a system reminder says ultracode is on, or on /nh:qa-cell <ask>, and the answer is one notebook cell.',
  phases: [
    { title: 'Write', detail: 'nh:cell-writer writes and runs the one cell' },
    { title: 'QA', detail: 'nh:cell-qa reads code, real output, self-check and kernel variables' },
    { title: 'Revise', detail: 'nh:cell-writer edits the same cell from QA findings' },
  ],
}

// The agents inherit the session's model and effort, so no agent() call sets either option.
// The report is data for the main conversation; how to reply from it is in the nh:notebook
// skill (reference/qa-workflow.md), never in the report.
const WRITER = 'nh:cell-writer'
const QA = 'nh:cell-qa'
const MUST_FIX = ['blocker', 'major']
const MAX_QA_ROUNDS = 5 // safety cap; nh's [turn] max_revisions is the real limit

const WRITER_SCHEMA = {
  type: 'object',
  properties: {
    status: {
      type: 'string',
      enum: ['ok', 'error', 'running', 'queued', 'aborted', 'timeout', 'interrupted', 'deleted', 'lost', 'conflict', 'refused', 'no_write'],
    },
    wrote: { type: 'boolean' },
    result: { type: 'string' },
    changes: { type: 'string' },
    cell_title: { type: 'string' },
    cell_id: { type: 'string' },
    exec_count: { type: 'integer' },
    notebook: { type: 'string' },
    lead_lines: { type: 'array', items: { type: 'string' } },
  },
  required: ['status', 'wrote', 'result', 'changes'],
}

const QA_SCHEMA = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['pass', 'revise', 'fail', 'unchecked'] },
    summary: { type: 'string' },
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
          what: { type: 'string' },
          evidence: { type: 'string' },
          fix: { type: 'string' },
        },
        required: ['severity', 'what', 'evidence'],
      },
    },
    numbers_checked: { type: 'array', items: { type: 'string' } },
    lead_lines: { type: 'array', items: { type: 'string' } },
  },
  required: ['verdict', 'summary', 'findings'],
}

function clean(value) {
  if (typeof value === 'number') return String(value)
  return typeof value === 'string' ? value.trim() : ''
}
function list(value) {
  return Array.isArray(value) ? value : []
}
// nh's "--- next ---" section, and a refusal's "Next:" and "Writer:" lines, tell the tool's
// caller what to do. The workflow's caller gets the report as data, so they are dropped.
function stripNext(text) {
  let body = clean(text).replace(/\r\n?/g, '\n')
  body = body.replace(/(^|\n)--- next ---(?:\n[\s\S]*?)?(?=\n--- [^\n]+ ---(?:\n|$)|$)/g, '')
  if (/^nh: E\d{3}\b/m.test(body) && !/^nh: cell=/m.test(body)) {
    body = body.replace(/^(?:Next|Writer): [^\n]*(?:\n|$)/gm, '')
  }
  return body.trim()
}
// Revisions left, from the writer's machine line ("nh: cell=… revisions=1/2"); null when absent.
function revisionsLeft(done) {
  const text = clean(done.result)
  const machine = /^nh: cell=[^\n]*?\brevisions=(\d+)\/(\d+)/gm
  let match = null
  let last = null
  while ((match = machine.exec(text)) !== null) last = match
  return last ? Number(last[2]) - Number(last[1]) : null
}
// The first line of a result that is not one of its lead lines ("NEW kernel: …").
function headline(answer) {
  const lead = list(answer.lead_lines).map(clean)
  const lines = stripNext(answer.result)
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line && !lead.includes(line))
  return lines.length ? lines[0] : 'no nh result'
}

function readArgs(value) {
  if (typeof value === 'string') {
    const text = value.trim()
    if (text.startsWith('{')) {
      try {
        const parsed = JSON.parse(text)
        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) return parsed
      } catch {
        // not JSON: the whole string is the ask
      }
    }
    return { ask: text }
  }
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

const input = readArgs(args)
const ask = clean(input.ask)
const context = clean(input.context)
const target = clean(input.cell)
const notebook = clean(input.notebook)

const answers = [] // every agent answer, for lead lines
const writes = [] // writer answers that changed the notebook, in order
const rounds = [] // QA answers
const changes = []
const notes = []
let revisions = 0
let checked = -1 // index in writes of the version the last QA round saw
let uncertain = false // a revision may have changed the cell without saying so
let asked = [] // the findings the last revision was asked to fix

async function spawn(prompt, opts) {
  let answer = null
  try {
    answer = await agent(prompt, opts)
  } catch (error) {
    notes.push(`${opts.label} stopped: ${clean(String(error && error.message ? error.message : error))}`)
  }
  if (answer) answers.push(answer)
  return answer || null
}

function where() {
  return notebook ? `Notebook: ${notebook}.` : 'Notebook: the default one nh_inspect reports.'
}
function cellName(done) {
  const title = clean(done.cell_title) || 'the cell'
  return typeof done.exec_count === 'number' ? `"${title}" [${done.exec_count}]` : `"${title}"`
}
function writePrompt() {
  return [
    "Write: the one cell for the user's current message.",
    `The ask, in the user's words: ${ask}`,
    target ? `It changes the existing nh cell ${target}: use nh_edit_cell on that cell.` : 'Add it with nh_add_cell.',
    where(),
    context ? `What the main conversation already knows:\n${context}` : '',
  ]
    .filter(Boolean)
    .join('\n\n')
}
function fixesOf(qa) {
  return list(qa.findings).filter((finding) => finding && MUST_FIX.includes(finding.severity))
}
function findingList(fixes) {
  return fixes
    .map(
      (finding, i) =>
        `${i + 1}. [${finding.severity}] ${clean(finding.what)}\n   Evidence: ${clean(finding.evidence)}\n   Fix: ${clean(finding.fix) || 'your call'}`,
    )
    .join('\n')
}
function qaPrompt(done, round, asked) {
  return [
    `QA round ${round}: live-check ${cellName(done)} against the ask: ${ask}`,
    [done.cell_id ? `cell_id for nh_inspect: ${clean(done.cell_id)}.` : '', where()].filter(Boolean).join(' '),
    context ? `Context from the main conversation:\n${context}` : '',
    asked.length ? `The writer revised the cell to fix these findings of the last round; check each:\n${findingList(asked)}` : '',
    `What the writer did: ${clean(done.changes)}`,
    `The writer's last nh result, verbatim:\n${stripNext(done.result)}`,
  ]
    .filter(Boolean)
    .join('\n\n')
}
function revisePrompt(done, fixes, number) {
  return [
    `Revise (${number}): change ${cellName(done)} with nh_edit_cell` +
      (done.cell_id ? ` (cell_id ${clean(done.cell_id)}).` : '.') +
      ' Change no other cell.',
    `The ask, in the user's words: ${ask}`,
    where(),
    context ? `What the main conversation already knows:\n${context}` : '',
    `QA findings to fix:\n${findingList(fixes)}`,
    `Your previous nh result, verbatim:\n${stripNext(done.result)}`,
  ]
    .filter(Boolean)
    .join('\n\n')
}
function leadLines() {
  const lines = []
  for (const answer of answers) {
    for (const line of list(answer.lead_lines)) {
      const text = clean(line)
      if (text && !lines.includes(text)) lines.push(text)
    }
  }
  return lines
}

// outcome: no_ask | writer_failed (the writer returned nothing) | refused | not_written (no cell
// changed) | checked (QA checked the last version written) | not_checked (it did not).
function report(done, fixed) {
  const last = rounds.length ? rounds[rounds.length - 1] : null
  const sawFinal = Boolean(done && last && !uncertain && writes.length && checked === writes.length - 1)
  const finalChecked = sawFinal && last.verdict !== 'unchecked'
  const written = done && writes.length ? (finalChecked ? 'checked' : 'not_checked') : null
  const outcome = fixed || written || (done.status === 'refused' ? 'refused' : 'not_written')
  return {
    outcome,
    status: done ? done.status : null,
    cell: done
      ? {
          title: clean(done.cell_title),
          exec: typeof done.exec_count === 'number' ? done.exec_count : null,
          notebook: clean(done.notebook),
        }
      : null,
    result: done ? stripNext(done.result) : '',
    changes: changes.filter(Boolean),
    revisions,
    qa: {
      final_version_checked: finalChecked,
      verdict: sawFinal ? last.verdict : 'unchecked',
      summary: sawFinal ? clean(last.summary) : '',
      open_findings: sawFinal ? list(last.findings) : [],
      earlier_findings: !sawFinal && last ? list(last.findings) : [],
      numbers_checked: sawFinal ? list(last.numbers_checked) : [],
      rounds: rounds.map((qa, i) => ({ round: i + 1, verdict: qa.verdict, summary: clean(qa.summary) })),
    },
    lead_lines: leadLines(),
    notes,
  }
}

if (!ask) {
  notes.push('No ask was given. Usage: /nh:qa-cell <what the cell should do>.')
  return report(null, 'no_ask')
}

phase('Write')
let done = await spawn(writePrompt(), { label: 'cell-writer', phase: 'Write', agentType: WRITER, schema: WRITER_SCHEMA })
if (!done) {
  notes.push('The writer returned nothing; a cell may or may not have been written.')
  return report(null, 'writer_failed')
}
if (done.wrote) writes.push(done)
changes.push(clean(done.changes))

while (done.status === 'ok' && writes.length) {
  const round = rounds.length + 1
  phase('QA')
  const qa = await spawn(qaPrompt(done, round, asked), { label: `cell-qa ${round}`, phase: 'QA', agentType: QA, schema: QA_SCHEMA })
  if (!qa) {
    notes.push(`QA round ${round} returned nothing, so the last version was not checked.`)
    break
  }
  rounds.push(qa)
  checked = writes.length - 1
  const fixes = fixesOf(qa)
  log(`QA round ${round}: ${qa.verdict}, ${fixes.length} finding(s) to fix`)
  if (qa.verdict !== 'revise') break
  if (fixes.length === 0) {
    notes.push('QA asked for a revision but listed no blocker or major finding, so nothing was revised.')
    break
  }
  const left = revisionsLeft(done)
  if (left !== null && left <= 0) {
    notes.push('No revisions were left ([turn] max_revisions), so the findings stay open.')
    break
  }
  if (rounds.length >= MAX_QA_ROUNDS) {
    notes.push(`The workflow's limit of ${MAX_QA_ROUNDS} QA rounds was reached, so the findings stay open.`)
    break
  }
  phase('Revise')
  const revised = await spawn(revisePrompt(done, fixes, revisions + 1), {
    label: `cell-writer revision ${revisions + 1}`,
    phase: 'Revise',
    agentType: WRITER,
    schema: WRITER_SCHEMA,
  })
  if (!revised) {
    uncertain = true
    notes.push('A revision returned nothing; the cell may have changed after QA checked it.')
    break
  }
  if (!revised.wrote) {
    notes.push(`The revision changed nothing: ${headline(revised)}`)
    break
  }
  writes.push(revised)
  asked = fixes
  revisions += 1
  changes.push(clean(revised.changes))
  done = revised
}

if (done.status !== 'ok' && revisions > 0) {
  notes.push('The last revision left the cell not ok; one undo restores the notebook to before this message.')
}
const final = report(done, null)
log(`qa-cell: ${final.outcome}, status ${done.status}, ${revisions} revision(s), ${rounds.length} QA round(s)`)
return final
