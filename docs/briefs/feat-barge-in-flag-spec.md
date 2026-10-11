# Feature Spec: Barge-in behind a feature flag
Task: feat-barge-in-flag | Branch: feat/barge-in-flag | Date: 2026-10-11

## Summary
`VOICE_BARGE_IN=0|1` (default 0) renders `barge_in`. Off: an utterance whose VAD onset came while the server held a reply or announcement (wake cue excluded), with no abort or wake detect since, is transcribed and dropped (`barge-in disabled: dropped utterance during playback: ...`). No abort, no turn; the reply plays out and listening continues. On: today's behaviour.

## Changes
- `weixue_patches.py`: the VAD wrapper records the onset's playback state; the startToChat wrapper is the gate. With the flag off the echo guard only logs.
- `google_stt._speak`: no too-long or repeat line for a dropped utterance.
- `claude_agent` `turn_latency` line ends `barge_in=0|1`.
- `render_config.py`, template, `.env.example`, `docs/server.md` (Barge-in).
- Sim: `server/sim/barge_scenarios.sh` (no credentials); `agent_scenarios.sh` renders `VOICE_BARGE_IN=1` for its barge round.

## Design Decisions
- Playback includes the thinking phase (upstream's own barge-in condition), so off means no speech-triggered abort at all.
- Decided at the onset: an echo outlasting `tts stop` is still dropped; an abort (double-tap, BOOT) or wake detect after the onset makes the speech a normal turn.
- Speech that starts during the wake cue is the request with either value.

## Open Questions
- The firmware detects no wake word while speaking, so off leaves only double-tap and BOOT to stop a reply; a server-side "Jarvis during playback aborts" is possible.
- DESIGN.md ADR-7 still says barge-in is supported.

## Test Coverage
985 host tests pass; new `test_barge_in.py` covers both flag values and both LLM paths over vendored startToChat/handleAbortMessage, echo, abort/wake, wake cue and the metrics line. Sim 17/17 (`docs/logs/feat-barge-in-flag-sim-2026-10-11.log`): off drops the mid-reply utterance, the 90-frame reply plays out, the next utterance is answered; on cuts it at 55 frames and answers. Deferred to the orchestrator: deploy, then a live device round on both `VOICE_LLM` values (speech over a reply ignored, double-tap stops it, the follow-up answered); no device or audio was used.
