export const meta = {
  name: 'wave-gate',
  description: 'weixue wave runner: Opus developer per task in a sibling worktree, then a time-bounded gate (3 Sonnet lenses in parallel with the tester; findings deduped, capped, info skipped, warning=1 skeptic, error=2), one fix pass by default',
  whenToUse: 'Run a wave of weixue tasks. args = { tasks: [{ id, slug, branch, prompt, model? }], fixPasses?: 1, lensCap?: 5, extraCtx?: string }',
  phases: [ { title: 'Develop' }, { title: 'Gate' }, { title: 'Fix' } ],
}
const REPO = '/Users/jwang14/Projects/weixue'
const LOCK = 'DEVICE LOCK (one board, many agents): before any serial/flash use or a device-visible stack restart: until mkdir ~/Projects/weixue-vendor/.device-lock 2>/dev/null; do sleep 15; done; echo "$$ $(date)" > ~/Projects/weixue-vendor/.device-lock/owner; release with rm -rf ~/Projects/weixue-vendor/.device-lock (hold ≤25 min; break locks older than 40 min). Mac output volume for `say` stays at 55.'
const BASE = `You work ONLY in your sibling worktree (create: cd ${REPO} && git worktree add ../weixue-wt-<slug> -b <branch> main). Read DESIGN.md, CLAUDE.md, tasks.yml (your entry), docs/METHODOLOGY.md, docs/firmware.md, docs/server.md and any docs/briefs/<task>.md. Vendored upstream read-only at ~/Projects/weixue-vendor. Live backend ${REPO}/server (Up). Device /dev/cu.usbmodem2101 (verify; if 192.168.8.148 is unreachable report blocked). ${LOCK} End-to-end rule: exercise the change on the live stack and device (wake via say → STT → LLM → [HA] → TTS → audio), logs under docs/logs/ (redact tokens/SSID/BSSID). External-service design decisions (HA/ha-mcp policy, Google Cloud, Anthropic, GitHub, networking) are NOT yours: write question.json with options + recommendation. Deliverables: ONE squashed commit, .claude/status/dev-complete.json and spec.md (200-400 words); question.json if blocked on a user-only decision. Comments: final state only. Never print secrets or URLs containing "private_".`
const DEV = { type: 'object', properties: { status: { type: 'string', enum: ['complete','blocked','failed'] }, worktree: { type: 'string' }, commit: { type: 'string' }, summary: { type: 'string' }, question: { type: 'string' } }, required: ['status','worktree','summary'] }
const FINDINGS = { type: 'object', properties: { findings: { type: 'array', items: { type: 'object', properties: { title: { type: 'string' }, severity: { type: 'string', enum: ['error','warning','info'] }, file: { type: 'string' }, line: { type: 'integer' }, description: { type: 'string' }, fix: { type: 'string' } }, required: ['title','severity','file','description'] } } }, required: ['findings'] }
const VERDICT = { type: 'object', properties: { real: { type: 'boolean' }, severity: { type: 'string', enum: ['error','warning','info'] }, reason: { type: 'string' } }, required: ['real','severity','reason'] }
const TEST = { type: 'object', properties: { status: { type: 'string', enum: ['pass','fail'] }, checks: { type: 'array', items: { type: 'object', properties: { name: { type: 'string' }, result: { type: 'string', enum: ['pass','fail','skip'] }, detail: { type: 'string' } }, required: ['name','result','detail'] } }, summary: { type: 'string' } }, required: ['status','checks','summary'] }
const tasks = (args && args.tasks) || []
const FIX_PASSES = (args && args.fixPasses) ?? 1
const CAP = (args && args.lensCap) ?? 5
const EXTRA = (args && args.extraCtx) || ''
const RANK = { error: 0, warning: 1, info: 2 }
const ctx = (t) => `Worktree ${REPO.replace('weixue','weixue-wt-'+t.slug)} (branch ${t.branch}, base main). Read DESIGN.md, CLAUDE.md, tasks.yml entry ${t.id}, .claude/status/spec.md, docs/logs there, and git diff main..HEAD. Read-only on files. ${LOCK} Never print secrets. ${EXTRA}`
const LENSES = (t) => [
  { key: 'design-correctness', prompt: `${ctx(t)}\nLens: DESIGN.md/ADR conformance and the acceptance row for ${t.id}, plus correctness: read every changed file fully; logic/concurrency/timing bugs, ESP32 memory use, error paths, config keys vs upstream schema. Report at most ${CAP} findings, highest severity first; skip style nits.` },
  { key: 'security-ops', prompt: `${ctx(t)}\nLens: secrets (none committed; env/config paths only), network exposure, timeouts vs the device's 120 s channel timeout, restart/rollback behaviour, log redaction. At most ${CAP} findings, highest severity first.` },
  { key: 'tests-quality', prompt: `${ctx(t)}\nLens: are tests meaningful (not tautological), what acceptance-critical path is untested, comment rules (final state only), docs match code. At most ${CAP} findings, highest severity first.` },
]
async function gate(t) {
  const [lensResults, test] = await parallel([
    () => parallel(LENSES(t).map(l => () => agent(l.prompt, { label: `review:${t.id}:${l.key}`, phase: 'Gate', schema: FINDINGS, agentType: 'general-purpose', model: 'sonnet' }))),
    () => agent(`${ctx(t)}\nTester (hardware + end-to-end rules apply): run the task's tests; for firmware tasks acquire the device lock, flash THIS branch's build with firmware/flash.sh and exercise the feature on the device (speech via say at volume 55), capturing serial + docker logs; for server tasks run one live round on the stack; verify the acceptance row for ${t.id} in DESIGN.md; READ the task's own docs/logs and spec.md: if any recorded trial shows an acceptance criterion failing (e.g. 0/8 one-breath requests), that is a FAIL even if the developer marked the task complete; exactly one commit ahead of main; no secrets in diff/logs; release the lock. status=fail if acceptance-critical items fail or the task's own evidence contradicts its claims.`, { label: `tester:${t.id}`, phase: 'Gate', schema: TEST, agentType: 'general-purpose', model: 'sonnet' }),
  ])
  const raw = (lensResults || []).filter(Boolean).flatMap(r => r.findings).filter(f => f.severity !== 'info')
  const seen = new Set(); const uniq = []
  for (const f of raw.sort((a, b) => RANK[a.severity] - RANK[b.severity])) { const k = `${f.file}|${f.title.toLowerCase().slice(0, 40)}`; if (!seen.has(k)) { seen.add(k); uniq.push(f) } }
  const toVerify = uniq.slice(0, CAP * 2)
  if (uniq.length > toVerify.length) log(`${t.id}: ${uniq.length - toVerify.length} lower-severity findings not verified (cap)`)
  const confirmed = (await parallel(toVerify.map(f => () => {
    const n = f.severity === 'error' ? 2 : 1
    return parallel(Array.from({ length: n }, (_, i) => () => agent(`${ctx(t)}\nSkeptic #${i + 1}: reviewer claims ${JSON.stringify(f)}. Open the cited file and try to REFUTE it. real=true only if the defect exists as described and matters for this task; default false if uncertain.`, { label: `verify:${t.id}:${f.title.slice(0, 24)}`, phase: 'Gate', schema: VERDICT, agentType: 'general-purpose', model: 'sonnet' })))
      .then(vs => { const ok = vs.filter(Boolean); if (!ok.length) return null; const real = ok.every(v => v.real); if (!real) return null; const sev = ok.every(v => v.severity === 'error') && f.severity === 'error' ? 'error' : 'warning'; return { ...f, severity: sev } })
  }))).filter(Boolean)
  return { issues: confirmed, errors: confirmed.filter(f => f.severity === 'error'), test: test || { status: 'fail', checks: [], summary: 'tester produced no result' } }
}
phase('Develop')
const results = await pipeline(tasks,
  t => agent(`${t.prompt}\n\n${BASE}`, { label: `dev:${t.id}`, phase: 'Develop', schema: DEV, agentType: 'general-purpose', model: t.model || 'opus' }),
  async (dev, t) => {
    if (!dev || dev.status !== 'complete') return { task: t.id, status: dev ? dev.status : 'failed', dev }
    let g = await gate(t); let retries = 0
    while ((g.errors.length || g.test.status === 'fail') && retries < FIX_PASSES) {
      retries++
      const fix = await agent(`${t.prompt}\n\n${BASE}\n\n=== RETRY ${retries} FEEDBACK ===\nFix these confirmed issues/failing checks, amend into the single commit, rewrite dev-complete.json and spec.md:\n${JSON.stringify({ issues: g.issues, test: g.test })}`, { label: `fix${retries}:${t.id}`, phase: 'Fix', schema: DEV, agentType: 'general-purpose', model: t.model || 'opus' })
      if (!fix || fix.status !== 'complete') return { task: t.id, status: fix ? fix.status : 'failed', dev: fix || dev }
      g = await gate(t)
    }
    const pass = !g.errors.length && g.test.status === 'pass'
    log(`${t.id}: ${pass ? 'PASS' : 'FAIL'} after ${retries} fix pass(es), ${g.issues.length} confirmed issues`)
    return { task: t.id, status: pass ? 'pass' : 'fail', retries, dev, final: { issues: g.issues, test: g.test } }
  })
return results
