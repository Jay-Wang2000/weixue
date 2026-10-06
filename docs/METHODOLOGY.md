# Working methodology (weixue)

How this project is run, as practised in the 2026-10-06 session. Keep this current when the process changes.

## Roles
- **Jay**: sets goals and constraints, answers user-only questions (hardware, network, credentials, money, security posture, scope), approves every design decision about external services (Home Assistant, Google Cloud, Anthropic, GitHub, networking) after seeing options and a recommendation, performs voice/acceptance tests on the device, reviews milestones.
- **Orchestrator (main Claude session)**: owns `tasks.yml`, `DESIGN.md` ADRs and `docs/briefs/`; decides implementation details; merges; reports once per milestone. Does not do feature work itself.
- **Agents**: research scouts, developers, reviewers, skeptics, testers, run through the Workflow tool in this session (in-process, each action under the session's permission rules). No `claude -p` with blanket permission grants.
- **External reviewer**: OpenAI Codex (`gpt-6-astra`, reasoning high) reviews the spec and the merged code after every wave.

## Phases
1. **Research** before building: `/deep-research` skill structure (scope → parallel scouts with structured claims → two skeptics per brief → revision → synthesis). Reports live in `~/Projects/research/reports/` with evidence ledgers, and are published as claude.ai Artifacts. Decisions are written back into `DESIGN.md` as ADRs.
2. **Spec**: `DESIGN.md` (overview, architecture, feature table with acceptance, ADRs, milestones, open questions), `tasks.yml` (DAG, status, retries, questions), `CLAUDE.md` (hard rules). Codex reviews the spec before implementation starts; accepted fixes are applied, the rest become Open Questions.
3. **Briefs**: before a wave, one verified implementation brief per task (`docs/briefs/<task>.md`): facts with `file:line` or URL citations, skeptic-checked, with an explicit UNVERIFIED list.
4. **Wave execution** (one Workflow per wave):
   - Developer (Opus) in a sibling worktree `../weixue-wt-<slug>` on `feat/*` or `fix/*`, one squashed commit, writes `.claude/status/dev-complete.json` + `spec.md`, or `question.json` when blocked on a user-only decision.
   - Gate: 3–4 review lenses (Sonnet) → findings deduplicated by file+title and capped at the 5 highest-severity per lens; `info` is not verified, `warning` gets 1 skeptic, `error` gets 2 skeptics (Sonnet) trying to refute → confirmed issues only → independent tester (Sonnet). Errors or test failure trigger up to 2 fix passes.
   - End-to-end rule: every gate's tester runs the full live path (wake → STT → LLM → [HA] → TTS → device audio) using the live stack and `say`; mocked tests alone never pass a gate.
   - Hardware rule: firmware/UI/behaviour changes are flashed and exercised on the real device by the tester; speech is driven with macOS `say`; evidence (redacted logs, photos) goes under `docs/logs/`.
   - One board, many agents: the device lock `~/Projects/weixue-vendor/.device-lock` (mkdir to acquire) serialises serial/flash/stack-restart access.
5. **Merge**: local `git merge --no-ff` (or ff) into `main` while the personal remote is deferred; conflicting shared files are integrated in a dedicated `integrate/*` branch with its own gate. Orchestrator bookkeeping (`tasks.yml`, `docs/briefs/`, `docs/reviews/`, `docs/METHODOLOGY.md`) is committed directly on `main`.
6. **Post-wave review**: Codex reviews `main` (spec + code) read-only; output saved to `docs/reviews/codex-<model>-<scope>-<date>.md`; findings become fix tasks or ADR changes.
7. **Debrief**: milestone message to Jay: what merged, what is live, known issues, what is blocked on him.

## Conventions
- Models: Opus for developers and fix passes; Sonnet for reviewers, skeptics, testers, scouts; Fable only for synthesis or a single hard judgement. Wall-clock speed matters more than token price.
- Replies to Jay are short; detail lives in files and artifacts.
- Secrets never appear in prompts, logs, docs or commits; URLs containing `private_` and `token=` are redacted everywhere.
- Comments state final state only. No footers on PR bodies; commits by the orchestrator carry the Claude co-author line, agent commits need not.
- Codex invocation that works non-interactively: ASCII prompt in a file, `codex exec --skip-git-repo-check --sandbox read-only -m gpt-6-astra -c model_reasoning_effort=high -o <out.md> - < prompt.txt`. Inline long prompts or an open stdin hang or die with exit 137.
- Research skill: `/deep-research`; validator needs English section headers (use bilingual headings).

## Milestones so far
- 2026-10-06: research v1 + agent-backend research; spec v0.2 + codex spec review; Wave 1 (toolchain, server) merged; Wave 2 (firmware base, Google STT/TTS, Anthropic LLM) merged and live; `fix-server-polish` merged; Wave 3 + `feat-ha-mcp` in progress.
