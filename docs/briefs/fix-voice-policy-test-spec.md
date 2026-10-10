# Feature Spec: render_voice_policy idempotence
Task: fix-voice-policy-test | Branch: fix/voice-policy-test | Date: 2026-10-10

## Summary
The voice overlay is now applied in `~/Projects/ha-agent` (f974b89), so `test_overlay_applies_to_ha_agent_policy` failed on the live file. `render_voice_policy.py` treats an already-applied base as a no-op, and the tests use frozen snapshots instead of the live file.

## Changes
- `render_voice_policy.py`: new `is_applied(base, overlay)` (no removed rule present, every added rule present, overlay settings match base). `apply_overlay` returns the base unchanged in that case; the CLI prints `already applied` to stderr. Overlay validation (including the `wait_seconds` refusal) runs first and is shared. A base missing a removed rule without the added rules still raises.
- `tests/fixtures/ha_agent_policy_{pre,post}_overlay.json`: ha-agent `tool_policy.json` at f974b89^ (87 rules) and f974b89 (109 rules). Secret check: only tool names, regexes and one Meross entity id (already in the test file); no tokens, URLs or credentials.
- `test_voice_policy.py`: pre snapshot replaces the live file (merge result equals the post snapshot's rules); new tests for the already-applied path, a partially applied base, `wait_seconds` on an applied base, and the CLI stderr message. The live-bridge-domain check uses the pre snapshot.
- `docs/ha-mcp.md`: apply section notes the idempotent behaviour and fixtures; heading no longer says "not applied".

## Design Decisions
- Snapshots are frozen post-overlay too, so the tests never read `~/Projects/ha-agent`.

## Problems & Resolutions
- No system libopus; copied `libopus.dylib` into the git-ignored `.venv/lib/` as docs/server.md describes.

## Open Questions
None.

## Future Work
None.

## Test Coverage
Full server suite in a Python 3.12 venv: 744 passed, 12 skipped (live-API tests need RUN_LIVE_TESTS). The CLI run against the live ha-agent file prints `already applied`. Deferred: no deploy, device, audio or end-to-end run; this task changes a host-side script and its tests only, and the policy was applied separately on 2026-10-10.
