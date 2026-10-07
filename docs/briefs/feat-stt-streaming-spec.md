# Feature Spec: Streaming STT (latest_short)
Task: feat-stt-streaming | Branch: feat/stt-streaming | Date: 2026-10-07

## Summary
New provider `google_stt_stream` (STREAM type) is now the selected ASR. Speech-to-Text v2 StreamingRecognize with `latest_short`, `us`, en-US + cmn-Hans-CN and interim results. Audio streams from the first voiced frame, the stream is half-closed on the VAD stop, and the transcript comes from the stream. The chirp_3 batch provider stays configured as the fallback and as the switch-back option.

## Changes
- `overlay/core/providers/asr/google_stt_stream.py`: subclasses the batch provider and adds a per-utterance stream on its own thread. Errors, timeouts and Chinese finals with low confidence are re-recognized by chirp_3 batch. Each turn logs `stt_final_latency_ms=… model=… fallback=…`.
- `google_stt.py`: `_build_config` and `_recognize` take a `model`.
- `config.template.yaml`: `selected_module.ASR: GoogleSTTStream`. `GoogleSTT` is unchanged. No new env.
- Tests: 21 unit tests with a fake streaming client and 2 live tests. New accuracy script `scripts/stt_accuracy.py`. New `sim/docker-compose.stt.yml` and `sim/stt.config.yaml`. Updated `docs/server.md`.

## Design Decisions
- `latest_short` ends a stream at its own end of utterance (measured live). If the VAD still hears speech, a follow-on stream opens and the finals are joined. Verified live: two sentences with a 2 s pause gave both sentences, `streams=2`.
- `zh_fallback_confidence: 0.9`. Mandarin finals always report confidence 0.91, so with this value the Chinese fallback never fires.

## Problems & Resolutions
- The api_core first-response prefetch would block cancel, so it is disabled, as in google_tts.

## Open Questions
- latest_short got code-switched Chinese ("living room" → 李炳荣) and 雾量 wrong; chirp_3 got both right. No signal routes these to chirp_3. Accept, or re-recognize all Chinese utterances (+~1.4 s)?
- Google's v2 language table lists no `short` model for cmn-Hans-CN, yet the API accepts it and returns `cmn-hans-cn`.

## Test Coverage
654 host tests pass. Live: English 226 ms, Mandarin 241 ms to the final. Accuracy (`docs/logs/feat-stt-streaming-accuracy-2026-10-07.md`): latest_short 0.0% WER/CER on 10+10 sentences, the same as chirp_3. Finals arrive 220/300 ms after the half-close, against 1.36–1.45 s for batch. One sim round per language (`docs/logs/feat-stt-streaming-sim-2026-10-07.log`): `stt_final_latency_ms=185` (en) and `452` (zh).

Deferred to the milestone E2E session (live stack + device): microphone accuracy, speech-end → first audio ≤ 1.5 s, barge-in and echo guard with the STREAM provider, wake word followed by a pause (follow-on stream), live deploy.
