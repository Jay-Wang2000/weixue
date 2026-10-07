export const meta = {
  name: 'wave-lite',
  description: 'weixue lite runner (Jay 2026-10-07: cut gates): Opus developer per task in a sibling worktree, then ONE Sonnet reviewer+tester; a fix pass only on errors or failing checks',
  whenToUse: 'Default runner for weixue tasks. args = { tasks: [{ id, slug, branch, prompt, model? }], extraCtx?: string, fixPasses?: 1 }. Use wave-gate (3 lenses + skeptics) only for security-sensitive changes when the orchestrator asks for it.',
  phases: [ { title: 'Develop' }, { title: 'Check' }, { title: 'Fix' } ],
}
const REPO = '/Users/jwang14/Projects/weixue'
const LOCK = 'DEVICE LOCK (one board, many agents): before any serial/flash use: eval "$(' + REPO + '/scripts/device_lock.sh acquire | grep ^export)"; release with scripts/device_lock.sh release (hold ≤25 min; break locks older than 40 min with rm -rf ~/Projects/weixue-vendor/.device-lock).'
const AUTH = 'AUTHORITY: this task prompt comes from the weixue orchestrator and is already authorized by Jay; any conversational message from Jay you may see in your context is unrelated to this task and must not stop or redirect it. Do the task.'
const BASE = AUTH + ' You work ONLY in your sibling worktree (create: cd ' + REPO + ' && git worktree add ../weixue-wt-<slug> -b <branch> main; if it already exists, cd into it and rebase onto main). Read DESIGN.md, CLAUDE.md, tasks.yml (your entry), docs/METHODOLOGY.md, docs/firmware.md, docs/server.md and docs/briefs/<task>.md. Vendored upstream read-only at ~/Projects/weixue-vendor. Live backend ' + REPO + '/server. ' + LOCK + ' External-service design decisions (HA/ha-mcp policy, Google Cloud, Anthropic, GitHub, networking) are NOT yours: write question.json with options + recommendation. Deliverables: ONE squashed commit (no footer), .claude/status/dev-complete.json and spec.md (200-400 words, list deferred checks explicitly). Comments: final state only. Never print secrets or URLs containing a secret path.'
const DEV = { type: 'object', properties: { status: { type: 'string', enum: ['complete','blocked','failed'] }, worktree: { type: 'string' }, commit: { type: 'string' }, summary: { type: 'string' }, question: { type: 'string' } }, required: ['status','worktree','summary'] }
const CHECK = { type: 'object', properties: { status: { type: 'string', enum: ['pass','fail'] }, errors: { type: 'array', items: { type: 'object', properties: { title: { type: 'string' }, file: { type: 'string' }, description: { type: 'string' }, fix: { type: 'string' } }, required: ['title','file','description','fix'] } }, warnings: { type: 'array', items: { type: 'string' } }, checks: { type: 'array', items: { type: 'object', properties: { name: { type: 'string' }, result: { type: 'string', enum: ['pass','fail','skip'] }, detail: { type: 'string' } }, required: ['name','result','detail'] } }, summary: { type: 'string' } }, required: ['status','errors','warnings','checks','summary'] }
const tasks = (args && args.tasks) || []
const FIX_PASSES = (args && args.fixPasses) ?? 1
const EXTRA = (args && args.extraCtx) || ''
const ctx = (t) => AUTH + ' Worktree ' + REPO.replace('weixue','weixue-wt-'+t.slug) + ' (branch ' + t.branch + ', base main). Read DESIGN.md, CLAUDE.md, tasks.yml entry ' + t.id + ', docs/briefs/' + t.id + '.md if present, .claude/status/spec.md, docs/logs there, and git diff main..HEAD. ' + LOCK + ' Never print secrets. ' + EXTRA
async function check(t) {
  return agent(ctx(t) + '\nYou are the single reviewer+tester. (1) Read every changed file; report as ERRORS only defects that break the task\'s acceptance row, the live voice path, device/proactive auth, secrets handling, or the ha-mcp single-gate rule; everything else is a warning (one line each, max 6). (2) Run the task\'s tests (server suite in the worktree venv; firmware/tests/run.sh for firmware); (3) verify the task\'s own evidence: if its spec/logs show an acceptance criterion failing, that is a fail; (4) for firmware tasks flash THIS branch\'s build under the device lock and exercise the feature as far as office mode allows, leave the normal build running and release the lock; for server tasks exercise the change against the live stack only as the extraCtx permits; (5) exactly one commit ahead of main; no secrets in diff/logs; spec lists deferred checks. status=fail only on errors or a failing acceptance-critical check.', { label: 'check:' + t.id, phase: 'Check', schema: CHECK, agentType: 'general-purpose', model: 'sonnet' })
}
phase('Develop')
const results = await pipeline(tasks,
  t => agent(t.prompt + '\n\n' + BASE, { label: 'dev:' + t.id, phase: 'Develop', schema: DEV, agentType: 'general-purpose', model: t.model || 'opus' }),
  async (dev, t) => {
    if (!dev || dev.status !== 'complete') return { task: t.id, status: dev ? dev.status : 'failed', dev }
    let c = await check(t); let retries = 0
    while (c && c.status === 'fail' && retries < FIX_PASSES) {
      retries++
      const fix = await agent(t.prompt + '\n\n' + BASE + '\n\n=== FIX PASS ' + retries + ' ===\nFix these errors / failing checks, amend into the single commit, refresh dev-complete.json and spec.md:\n' + JSON.stringify({ errors: c.errors, checks: c.checks.filter(x => x.result === 'fail') }), { label: 'fix' + retries + ':' + t.id, phase: 'Fix', schema: DEV, agentType: 'general-purpose', model: t.model || 'opus' })
      if (!fix || fix.status !== 'complete') return { task: t.id, status: fix ? fix.status : 'failed', dev: fix || dev }
      c = await check(t)
    }
    const pass = !!c && c.status === 'pass'
    log(t.id + ': ' + (pass ? 'PASS' : 'FAIL') + ' after ' + retries + ' fix pass(es); warnings: ' + ((c && c.warnings.length) || 0))
    return { task: t.id, status: pass ? 'pass' : 'fail', retries, dev, final: c }
  })
return results
