# Feature Spec: Codex M2 review fixes — gateway, STT, firmware, setup
Task: fix-m2-review-gateway | Branch: fix/m2-review-gateway | Date: 2026-10-11

## Summary
Closes Codex M2 F03, F04, F09, F10, F11, F13, F14 and F15. Partial STT text never becomes a command. "Approved" shows only after agent-service recorded the decision. Speech plays only after its hand-off was recorded, and never twice across restarts. A restarted gateway restores cards before any pruning sync.

## Changes
- F03: after a stream error or timeout only a complete-buffer chirp_3 transcript counts; if it fails, no turn and `repeat_reply` is spoken.
- F04: approve/deny/answer receipts wait for the service (12 s, card-scoped decision id); `stale` otherwise.
- F09/F10: `accepted` is answered before queueing; acks are ordered and retried; one terminal ack per attempt; the service owns retries. `data/weixue-gateway/` keeps hand-offs, decisions, wire seqs, unsent acks and proactive keys.
- State files are fsynced (file and directory); `<name>.prev` keeps the previous generation and is read when the current one fails to parse. A hand-off the file could not record is not played (acked `deferred`).
- A delivery with a handed-off attempt is never spoken in a later attempt; one that ended `deferred` before audio is spoken in the next.
- F11: the device snapshot restores mirror and chats before the stream opens; the snapshot GET is bounded (4 s) and retried with the stream's backoff.
- F13: INTERRUPT cuts the playing agent sentence (`superseded`), aborts the voice turn, then plays.
- F14: setup.sh is one transaction from the first `.env` change: it restores .env, the endpoint, images, the DB move and the stopped services.
- F15: patch 36 sends `weixue:proactive:<id>:<lease>`; the server rejects bare markers from level 2.

## Design Decisions
- Card-scoped decision ids: a re-tap after a lost reply gets the stored answer.
- A hand-off save failure after the service's 2xx is acked `deferred`, so the service offers the next attempt instead of holding a `handed_off` row that never plays.
- Restored cards reuse persisted wire seqs, so the device keeps its seen state.

## Problems & Resolutions
- Rebased after fix-m2-review-service merged. Its DB backup now lives inside the setup transaction, and the protocol docs were merged.
- Review: an attempt 2 after a `lost` attempt 1 was spoken; the hand-off check now covers every later attempt.

## Open Questions
- Deploy server and firmware together: the new server rejects old level-2 firmware's bare invocation.

## Future Work
- Live check of F03/F04/F09/F11/F13 once deployed. Failed-stream audio is covered by the milestone audio session.

## Test Coverage
Server: 952 passed. Agent-service: 137 passed. Firmware host tests pass. New tests cover streaming STT (fake client), gateway recovery (incl. hung decision, `accepted` and snapshot requests; state-file fallback and save failure; deferred then next attempt across a restart), proactive binding and persistence, and setup.sh (stub docker). Scratch-service sim rounds: 20/20 for each of three service versions. Board (loopback, volume 0): 3/3 lease-bound invocations and a receipt round-trip. The board runs the previous revision's release build; this amend changes no firmware.
