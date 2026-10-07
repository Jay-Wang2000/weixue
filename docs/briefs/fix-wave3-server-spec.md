# Feature Spec: Server fixes from Codex Wave 3 review
Task: fix-wave3-server | Branch: fix/wave3-server | Date: 2026-10-07

## Summary
Gateway fixes S1, C4-C7, Wave 4 urgency, echo guard, `wait_seconds` cleanup. Rebased onto `main` with fix-server-review-2; host tests pass (546); simulated-device run passed. **No live deploy was allowed in this run.** Live checks are batched into the orchestrator's post-merge live step; the task completes only when they pass.

## Changes
- S1: poll, session registry and `weixue:proactive` invocation accept only identities the WebSocket boundary admits (`weixue_patches.unlisted`, shared with review-2's OTA/WS wrappers): listed `Device-Id`, exact match; OTA token only with `allowed_devices` empty. `401`/`503` otherwise. Liveness = connection lifecycle plus a ping probe. Polls and devices bounded.
- C6 + Wave 4: a poll reserves one message (15 s) and returns its `id` and `urgent`; the invocation delivers only that message; claimed before its task exists.
- C7: outcome from the sentence's own playback record; no audio → requeue after 2 s / 5 s; cut-off audio or a part whose synthesis failed (Google TTS raised, or a segment got no audio) → `partial`.
- C4: wake cue fenced by turn, rechecked after every await, cancelled on new wake, turn start and session end.
- C5 + echo guard: lexical cue suppression removed; a transcript whose utterance began during a reply sentence and matches it is dropped before barge-in. Wake cues are never echo candidates.
- Policy: overlay may not set `wait_seconds`.
- `server/sim/`: credential-free sim stack (`docs/logs/fix-wave3-server-sim-2026-10-07.log`).

## Design Decisions
- Urgency bound by reservation, not a new device message.
- Echo window from VAD onset, not transcript arrival.
- Wake-cue captions are not echo candidates: "Yes." after "Yes?" is an answer; a cue echo reaches the LLM.

## Problems & Resolutions
- Rebase onto review-2: test conflicts merged keeping both sides; the poll accepted an unlisted device holding a valid token and case-folded ids, which review-2's WebSocket refuses. Now one predicate; a parity test runs both against upstream `_handle_auth`.

## Open Questions
- A barge-in that repeats the opener ("turn off the valve") is dropped as echo.

## Future Work
- Option D device credential (firmware sends token on poll).

## Live checks for the orchestrator step (after fix-server-review-2, `setup.sh`)
1. `curl` poll, unlisted `Device-Id` → 401.
2. Announce from idle → poll 200 with `id`; `reserved for 7c:0c:5f:42:4b:fc`; `delivered`.
3. Urgent conversation → bedside shows face; say "Yes" → `answered`, reply "Yes.".
4. HA: "Jarvis, turn on the living room light" → light on.
5. Loop "Jarvis" then "Yes" until each cue (`wake cue: Hmm?` / `Mm-hm?` / `Yes?`) has played; every "Yes" reaches the LLM, 0 `playback echo ignored`.
6. Long reply, nobody speaking → 0 `Abort message received`.
Logs to `docs/logs/`.

## Test Coverage
New tests: poll/WebSocket auth parity, retry backoff, mid-stream and later-chunk synthesis failure → `partial` (real Google TTS threads), every template cue followed by short answers through the echo guard, device auth, reservation TTL/urgency, overlapping invokes, stale claims, ping eviction, poll bounds, cue races and cancellation, echo matching (English, Chinese, short answers, onset window), the policy rule, and delivery through the real Google TTS threads and upstream `sendAudioMessage` (`InvalidArgument` → requeue; lost session → `partial`). The sim covers HTTP/WS paths, the three cues + "Yes." and a mid-stream synthesis failure. No device, audio or HA round ran.
