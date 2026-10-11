# Feature Spec: Audio session driver speaks Google TTS
Task: fix-session-tts | Branch: fix/session-tts | Date: 2026-10-11

## Summary
`scripts/audio_session.py` no longer uses macOS `say`. Every utterance, including the wake word, is a Google Cloud TTS Chirp 3 HD clip cached under `~/Projects/weixue-vendor/session-clips/` and played with `afplay` at volume 55. One-breath recognition on the board is 10/10 with the new default voice.

## Changes
- `scripts/session_tts.py`: voice mapping (speaker to `en-US-`/`cmn-CN-Chirp3-HD-<speaker>`, language from the text), 24 kHz LINEAR16 synthesis with 250 ms silence pads, hash-keyed cache, `afplay` command.
- `audio_session.py`: utterance table, `--voice <speaker|voice name>` (default Fenrir), `--synth-only`.
- `audio_session.sh`: reads `GOOGLE_SA_FILE` from the live `.env`, exports `GOOGLE_APPLICATION_CREDENTIALS`, installs the pinned TTS library into `.venv`, warms the cache before taking the lock; `--synth-only`; `--no-audio` unchanged.
- `scripts/tests/test_session_tts.py` (also run by `firmware/tests/run.sh`), `docs/firmware.md`.

## Design Decisions
- Default voice Fenrir. On the same board and build, wake plus request in one breath: Charon 1/10, Aoede 5/6, Fenrir 6/6 then 10/10 in the full run.

## Problems & Resolutions
- Charon clips woke the device in 4 of 10 trials and lost the request after the wake word; switching speaker fixed it.

## Open Questions
- wake_cue 1/3, long_reply and barge_in still FAIL (run 0748). The wake pre-roll is transcribed as "Jervis"/"service"/"Cervix" and becomes an LLM turn, so the driver's first-transcript check sees it instead of "Yes."; long_reply: the wake word was not detected; barge_in: the interrupting "Stop. What is two plus two?" was transcribed "der ist die". The STT dropout and driver judging work is already queued; the clips themselves play correctly.

## Future Work
- Chinese session items (the cmn-CN mapping is tested, no item uses it yet).

## Test Coverage
16 unit tests: cache key, voice mapping, padding, single synthesis per text, `afplay` command through `Session.say` with subprocess mocked, `--no-audio` plays and synthesises nothing, no literal utterances outside the table. One smoke test synthesises a clip with real credentials (passes; skipped without them). On-device runs: `docs/logs/session-2026-10-11-0719.md` (Charon) and `-0748.md` (Fenrir).

## Deferred checks
- Device voice items other than one_breath pass only after the STT dropout and driver judging fixes; re-run then.
- Chinese clip playback is not exercised on the board.
