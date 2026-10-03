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
      enum: [
        'ok',
        'error',
        'running',
        'queued',
        'aborted',
        'timeout',
        'interrupted',
        'deleted',
        'lost',
        'conflict',
        'refused',
        'no_write',
        'needs_approval',
      ],
    },
    wrote: { type: 'boolean' },
    result: { type: 'string' },
    changes: { type: 'string' },
    cell_title: { type: 'string' },
    cell_id: { type: 'string' },
    exec_count: { type: 'integer' },
    notebook: { type: 'string' },
    lead_lines: { type: 'array', items: { type: 'string' } },
    // With needs_approval: the call nh refused with E122, exactly as the writer sent it.
    call: {
      type: 'object',
      properties: {
        tool: { type: 'string', enum: ['nh_add_cell', 'nh_edit_cell'] },
        code: { type: 'string' },
        cell_id: { type: 'string' },
        after_cell_id: { type: 'string' },
        notebook: { type: 'string' },
        title: { type: 'string' },
        notes: { type: 'array', items: { type: 'string' } },
        intent: { type: 'string' },
      },
      required: ['tool', 'code'],
    },
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
// A result's lines. Only LF ends one (CR LF and a lone CR read as LF): JavaScript's `.` and
// multiline `$` also stop at U+2028 and U+2029, which a path in nh's question may hold.
function linesOf(answer) {
  return clean(answer.result).replace(/\r\n?/g, '\n').split('\n')
}
// Whether the writer's last nh result is an E122: nh asked, or would ask, for the user's yes.
function isE122(answer) {
  return linesOf(answer).some((line) => /^nh: E122\b/.test(line))
}
// nh's question for the user in a writer's E122 (design §6.4): the gateway words it, and the
// workflow only carries it, unchanged. '' when the result holds none.
function questionOf(answer) {
  if (!isE122(answer)) return ''
  let question = ''
  for (const line of linesOf(answer)) {
    const asks = /^- The main conversation asks the user: '([^\n]*)'[ \t]*$/.exec(line)
    if (asks) question = asks[1]
  }
  return question
}
// The call nh asked about, as the main conversation sends it after the user's yes: the code
// verbatim (the approval key reads it as sent), and only the arguments the tool takes.
const CALL_ARGS = {
  nh_add_cell: ['title', 'notes', 'intent', 'after_cell_id', 'notebook'],
  nh_edit_cell: ['cell_id', 'title', 'notes', 'intent', 'notebook'],
}
function callOf(answer) {
  const call = answer.call
  if (!call || typeof call !== 'object' || Array.isArray(call)) return null
  const keys = Object.prototype.hasOwnProperty.call(CALL_ARGS, call.tool) ? CALL_ARGS[call.tool] : null
  if (!keys || typeof call.code !== 'string' || !call.code.trim()) return null
  const args = { code: call.code }
  for (const key of keys) {
    if (key === 'notes' && Array.isArray(call.notes)) {
      const notes = call.notes.map(clean).filter(Boolean)
      if (notes.length) args.notes = notes
    } else if (clean(call[key])) {
      args[key] = clean(call[key])
    }
  }
  if (call.tool === 'nh_edit_cell' && !args.cell_id) return null
  return { tool: call.tool, args }
}
// E122's other writer lines (design §6.4): no question for this run's call, so nothing to ask.
const E122_LINES = [
  [
    "- nh is already waiting for the user's answer",
    "nh was already waiting for the user's answer to an earlier question of this message, which the writer didn't return, so there is nothing to ask.",
  ],
  [
    "- The user's yes in this message is for the other cell nh asked about",
    "nh keeps this message's yes for the other cell the user approved, so it didn't write the writer's cell.",
  ],
  [
    '- No one can answer here (NH_HEADLESS=1)',
    "No one can answer here (NH_HEADLESS=1): the cell needs the user's yes in an interactive session.",
  ],
]
// The approval a writer answer carries: nh's question and the exact call, or null. Its notes
// say what was missing when the writer said nh asked but the report can't carry it.
function approvalOf(answer) {
  const question = questionOf(answer)
  const call = callOf(answer)
  if (question && call) return { question, tool: call.tool, args: call.args }
  const lines = isE122(answer) ? linesOf(answer) : []
  const other = E122_LINES.find(([start]) => lines.some((line) => line.startsWith(start)))
  if (question) {
    notes.push("nh asked for the user's yes (E122), but the writer didn't return the exact call, so there is no call to send after a yes.")
  } else if (other) {
    notes.push(other[1])
  } else if (answer.status === 'needs_approval') {
    notes.push("The writer said nh asked for the user's yes, but its nh result holds no question from nh, so there is nothing to ask.")
  }
  return null
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
let approval = null // nh's question and the exact call, when nh asked before writing (E122)

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

// outcome: no_ask | writer_failed (the writer returned nothing) | needs_approval (nh asked for
// the user's yes before writing the writer's last call: approval holds nh's question and that
// exact call) | refused | not_written (no cell changed) | checked (QA checked the last version
// written) | not_checked (it did not).
function report(done, fixed) {
  const last = rounds.length ? rounds[rounds.length - 1] : null
  const sawFinal = Boolean(done && last && !uncertain && writes.length && checked === writes.length - 1)
  const finalChecked = sawFinal && last.verdict !== 'unchecked'
  const written = done && writes.length ? (finalChecked ? 'checked' : 'not_checked') : null
  const refusal = done && ['refused', 'needs_approval'].includes(done.status) ? 'refused' : 'not_written'
  const outcome = fixed || (approval ? 'needs_approval' : null) || written || refusal
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
    approval,
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
approval = approvalOf(done)
if (approval) notes.push("nh asked for the user's yes before writing the writer's last call (E122), so QA checked nothing.")
if (done.wrote && isE122(done)) {
  notes.push("The cell the writer wrote before nh's E122 is in the notebook, not QA-checked; one undo removes it.")
}

// QA checks only a cell nh wrote: never when the writer's last call got E122 (waiting for the
// user's yes, or refused one), whatever status the writer gives.
while (!isE122(done) && done.status === 'ok' && writes.length) {
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
  approval = approvalOf(revised)
  if (approval && !revised.wrote) {
    changes.push(clean(revised.changes)) // what the revision nh asked about would change
    notes.push(`nh asked for the user's yes before writing revision ${revisions + 1} (E122), so the cell is still the version QA checked.`)
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
  if (approval) {
    notes.push(`nh asked for the user's yes before writing the writer's fix of revision ${revisions} (E122), so QA didn't check it.`)
    break
  }
  if (isE122(revised)) {
    notes.push(`nh's E122 ended the writer's fix of revision ${revisions}, so QA didn't check it.`)
    break
  }
}

if (done.status !== 'ok' && revisions > 0) {
  notes.push('The last revision left the cell not ok; one undo restores the notebook to before this message.')
}
const final = report(done, null)
log(`qa-cell: ${final.outcome}, status ${done.status}, ${revisions} revision(s), ${rounds.length} QA round(s)`)
return final
