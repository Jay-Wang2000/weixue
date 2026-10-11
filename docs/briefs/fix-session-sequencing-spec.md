# Feature Spec: Session driver sequencing and a real earcons approval
Task: fix-session-sequencing | Branch: fix/session-sequencing | Date: 2026-10-11

## Summary
The driver now plays a wake clip only on an idle device that has stayed settled for 1.5 s. If the device stays non-idle, the trial is recorded as `not_idle`. `earcons` is now its own item. It drives one real held call through the agent service and approves it with a tap. Without `VOICE_LLM=claude_agent` it is skipped and gives the reason.

## Changes
- `wait_ready` (`audio_session.py`) runs before every wake clip:
  - waits for idle (serial state, or `diag dump` before the first state line);
  - then 1.5 s with no state change, `agent ui: pose=` or `diag lit=` snapshot;
  - gives up when one state lasts 30 s, or after 180 s in all.
- `long_reply` says "Jarvis" alone, then the request after the cue. The one-breath clip got no wake on 4/4 runs, from a settled idle device too (1107, 1136).
- `earcons` (`judge_earcons`):
  - opens a new non-current chat and asks for `ha_call_service` toggle on `input_boolean.weixue_session_probe`. That entity does not exist (checked read-only). Decision 57 names no tool.
  - polls until the hold can be approved, then taps `diag card approve` (up to 3 taps).
  - judges `needs_you`, card open, receipt `accepted`, `approved`, and the replayed call. "result=applied" is not a receipt value.
- `AgentClient` calls the API with `docker exec agent-service python`, so the token never leaves the container.
- `audio_session.sh` passes `--voice-llm`. Docs updated in `docs/firmware.md`.

## Open Questions
- **Blocker for earcons passing (agent service, not this branch):** `weixue_hamcp` `HaMcpClient(timeout_s=15)` sets the httpx read timeout to 15 s. ha-mcp holds for `wait_seconds` 20 s, so the held call ends `result unknown (ReadTimeout)` before its token arrives (1107: `no_hold`). Proposed fix: a per-request read timeout of `read_timeout_s`.

## Test Coverage
- 14 new tests; 59 script tests pass.
- Covered: idle reached, settle restart (state, pose, snapshot), per-state timeout, dump path, every voice item waiting, earcons pass, stale, skip, lost hold, and the transport.
- Board run `session-2026-10-11-1150`: 3/3. long_reply 53 s, wake_cue 3/3, 0 wake clips on a non-idle device.

## Deferred checks
- earcons pass on the board (needs the ReadTimeout fix).
- not_idle on the board.
- bedside_voice, barge_in and one_breath with the wait.
