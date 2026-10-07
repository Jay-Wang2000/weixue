# Feature Spec: Firmware fixes from Codex Wave 3 review
Task: fix-wave3-firmware | Branch: fix/wave3-firmware | Date: 2026-10-07

## Summary
Capture is continuous from wake word into listening (pre-refactor first run: 20/20 one-breath, 19/20 paused). Mute always closes the transport, thinking waits for speech, panel sends never block, the clock is UTC plus a POSIX TZ. Retry 1 moved decisions into host-tested headers and fixed torn IMU counter reads; voice acceptance is deferred to the home audio gate.

## Changes
- Patch 23: wake goes straight to voice processing; `WakeAudioHold` (50 packets) sends the full wake word.
- Patch 24: `MuteFence`/`ActionsOnMute`: transport closed, stale callbacks dropped, tts/listening/mic refused while muted.
- Patch 25 + `ThinkingTurn`, `ReplyText`: thinking until speech or a terminal state.
- Patch 26: device events via a worker session (4 s timeout, logged only); `PollAgeAt`/`BackendRowFor`.
- Patch 27 + `weixue_clock.h`: TZ from `server_time.tz`; `ResyncFromDate`; strict Date parsing.
- Patch 28 + `ImuPoll`: gate/resync; `AgreedCounter` re-reads TIMESTAMP.
- Server: `weixue_clock.py` sends `tz` only when `TZ` is set in `server/.env` (`WEIXUE_TZ`); `setup.sh` requires `TZ`.

## Acceptance mapping
| Item | State |
|---|---|
| C9 IMU resync, torn reads | done (host + device injection) |
| C1/C8/panel/reply logic | done (host) |
| Clock POSIX TZ, no UTC0 fallback | done (host + server tests) |
| 60 s answer, 0 self-aborts | owned by fix-wave3-server (echo guard); measured at home |
| Voice and device items below | deferred |

## Deferred to the home audio gate (feat-voice-integration)
- One-breath "Jarvis, what time is it?" ×10 (≥9).
- First transcript free of wake-word residue ≥9/10. Patch 23 sends the full wake word, so fragments like "Chavez" should disappear and the server's `checkWakeupWords` drops a wake-only transcript; to be measured.
- Wake-triggered mute invariants.
- Thinking state during a real turn.
- Panel send during a live conversation.
- 60 s long answer, 0 self-aborts.
- Face-down ×10 and double-tap ×10 with serial capture.
- Dublin TZ boot on the device.

## Problems & Resolutions
- Injection found torn TIMESTAMP reads; the re-read fixed them (resyncs equal injections).
- Compose defaults the container `TZ` to `UTC`, so an unset `.env` TZ sent `UTC0`; fixed via `WEIXUE_TZ` and the pre-flight.

## Open Questions
- Offline boot shows `--:--` (UTC clock not persisted). Persist, or accept?
- Date-header resync never fired on device.

## Test Coverage
Host: `session_test`, `imu_gestures_test`, `clock_test` (strict Date, resync, Dublin); server `test_clock.py` (unset/empty TZ → no `tz`, Dublin → `tz`, both OTA wrappers applied twice) and `setup.sh` pre-flight for `TZ`. The amend touches server/docs only; the firmware binary is unchanged.
