# Feature Spec: Streaming STT drops speech on the real device mic
Task: fix-stt-stream-dropouts | Branch: fix/stt-stream-dropouts | Date: 2026-10-11

## Summary
`latest_short` ends its single utterance at the pause after "Jarvis," (often labelled Mandarin: `Jarvis。`, `China。`) and drops the request. On quiet audio it also returns finals without alternatives. Pacing is not the cause. Those utterances now go to chirp_3 batch, wake-only segments are dropped before STT, and new probes, captures and a replay tool show what each stream received.

## Changes
- Stream provider:
  - New chirp_3 fallbacks `truncated`, `wake_word_only` and `no_final` (logged with its reason).
  - Follow-on streams start at the reported end offset.
  - `stt_stream` lines and drop reasons are logged.
  - No fallback for utterances that barge-in-off drops.
- Patches:
  - Wake-only segment drop (R2).
  - Live voice is counted by frame index after the pre-`listen start` burst.
  - `stt_segment` (kind, dBFS, burst), `audio session end packets/frames`, and `listen start without a wake detect`.
- `WEIXUE_STT_CAPTURE=1` captures (WAV + JSON), `scripts/stt_replay.py`, and a sim `--wake-audio` option.
- Firmware patch 38 (A1): `Session start: <reason>` and per-channel `packets sent/dropped/oldest age`.

## Design Decisions
- R3: the 0.9 threshold stays. Both 0.910 finals were wake-only or truncated segments, which are now handled.
- After `no_final`, an empty or failed fallback means no turn and no repeat prompt (as the RCA specifies).

## Problems & Resolutions
- A slowly drained wake burst was being counted as live voice. Live voice is now counted by frame index.

## Open Questions
- `question.json` (Jay): switch to `latest_long` streaming? On the replay sets it had 0/20 truncations, the same latency and the same Mandarin accuracy.
- R1 changes when chirp_3 is called. The orchestrator authorized it; Jay to confirm.
- A1 root cause is still open. Hypothesis: stale device audio carried into the next channel. The new counters can localize it.

## Future Work
- Stream only the audio after the wake word (needs a change to the firmware's detect order).

## Test Coverage
- 1031 server tests pass and firmware host tests pass. Diag and release builds succeed.
- Replay of 30 synthetic clips: WER stream+fallback 0.078, equal to batch (stream alone 0.272).
- Sim: one-breath 23/23; cue followed by "Yes." 6/6.
- Board: the session reasons and packet counts were logged and 3/3 replies came back.
- Deferred checks:
  - Live 10× one-breath on `GoogleSTTStream`, needs ≥9/10 (orchestrator deploy).
  - Device capture run with replay.
  - A1 repro with the counters.
  - R2 on the live batch stack.
