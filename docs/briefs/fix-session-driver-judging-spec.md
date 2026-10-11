# Feature Spec: Session driver judging from device and server events
Task: fix-session-driver-judging | Branch: fix/session-driver-judging | Date: 2026-10-11

## Summary
Every session item is judged by device console and server log events, not by guessing from the transcript. Each table row has a Reason column that lists the judged events (`L<n>` = serial line) with a failure class.

## Changes
- New `scripts/session_judge.py` holds the rules:
  - wake_cue: no_wake, cue_missing (no cue event on device or server), cue_late, preroll_turn, no_answer.
  - double_tap: gesture, then abort, then listening; a trial with no reply fails with "tap not sent".
  - long_reply: TTS start to stop, or no_transcript.
  - barge_in: follows VOICE_BARGE_IN.
  - bedside: tool-call events.
  - pick_up: speaker_vibration trials are not counted.
- `audio_session.py`: the items use the rules. `pick_up_dim` is now `pick_up`. New `--barge-in` option. Cue wait is 15 s.
- `audio_session.sh`: exports WEIXUE_PROACTIVE_TOKEN without printing it. Fixture base and device, and VOICE_BARGE_IN, come from the live `.env`.
- `DiagSerial.snapshot`; `docs/firmware.md`.

## Design Decisions
- A transcript other than "yes" counts as the wake pre-roll if it arrives before the "yes", or within 2 s of the "Yes." clip.

## Open Questions
- The pre-roll turn (RCA R2, server) wins every wake_cue trial on the board. The server then drops the "Yes." spoken during that turn's reply (barge-in off).

## Test Coverage
- 19 new tests; all 46 script tests pass.
- Fixtures are recorded excerpts of the 0655, 0719, 0748 and 0917 logs.
- The driver tests replay them through `Session`, with afplay, docker and the clock mocked.
- Board run with `--count 3` (`docs/logs/session-2026-10-11-0917.md`): wake_cue 0/3 (12 preroll_turn), double_tap 2/3, long_reply no_transcript (no wake), barge_in pass, pick_up 3/3.

## Deferred checks
- Not reached on the board, covered by fixtures only: a long_reply pass, a speaker_vibration trial, bedside_voice, card_tap/earcons.
- The 0917 table was made before the Reason column quoted the utterances the server dropped.
