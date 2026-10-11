# fix-stt-stream-dropouts: replay and sim evidence (2026-10-11)

No device audio of the 06:55 session exists (live `delete_audio: true`, no capture probe), so the
replay sets below are synthetic or sim-captured; the device evidence is the transcripts of the two
Google-TTS sessions. Device captures need the probe deployed on the live stack (spec, deferred checks).

## Device sessions with Google TTS clips (fix-session-tts runs, logs on main)

- `session-2026-10-11-0719` (latest_short streaming, voice Charon, build 621f64a): one_breath 1/10. Its 20 finals: `Jarvis.` x8 and `Jarvis。` x1 (the stream stopped after the wake word; a bare wake word is then dropped, so the request is lost), empty x7, `Yes.` x3, `What time is it?` x1.
- `session-2026-10-11-0748` (chirp_3 batch, voice Fenrir): one_breath 10/10 `Jarvis, what time is it?`; the wake-only segments came back as `Jervis` x5, `service` x4, `Cervix`, `der ist die` (each an LLM turn) and `Jarvis` x2; every `Yes.` after a cue (11) was recognized.

So on the real microphone latest_short loses the request after the wake word or returns nothing, while chirp_3 hears the whole utterance but turns the wake-only segment into junk turns. The branch handles the first with `truncated` / `wake_word_only` / `no_final` re-recognition and the second with the wake-only drop.

## latest_short failure modes found (raw responses, `server/scripts/stt_replay.py` + debug dumps)

| Mode | What the stream returns | Seen on |
|---|---|---|
| Truncated after the wake word | `END_OF_SINGLE_UTTERANCE` at the pause after "Jarvis," (often labelled `cmn-hans-cn`: `Jarvis。`, the session's `China。`), then the stream ends; the rest of the request is never recognized | one-breath "Jarvis, what time is it?" at every level, burst or real-time pacing |
| Final without alternatives | interims (`Jarvis, switch the screen.`), then a final result with no alternatives: text empty | quiet clips (peak -36 to -48 dBFS), real-time and burst |
| No response | no response at all for a short word | "Yes." at -48 dBFS |

Pacing is not the cause: the same clips give the same result sent in real time, at 2x/4x, or as one burst.

## Synthetic set (30 clips: 6 session phrases x peak -6/-26/-36/-42/-48 dBFS, Google TTS + noise floor)

| Pacing | latest_short stream WER / empty | chirp_3 batch WER / empty | stream + provider fallbacks (truncated, no_final) WER / empty | fallbacks taken |
|---|---|---|---|---|
| recorded (2.34 s leading burst on wake phrases) | 0.272 / 6 | 0.078 / 1 | 0.078 / 1 | 8 (2 truncated, 6 no_final) |
| real time | 0.294 / 6 | 0.078 / 1 | 0.078 / 1 | 9 (3 truncated, 6 no_final) |

The one empty clip with fallbacks is "Jarvis, switch the screen to bedside mode." at -48 dBFS, empty for batch too.

Other streaming models on the same set (recorded pacing; not configured, for decision only):

| Model | WER | empty | median final after half-close |
|---|---|---|---|
| latest_short | 0.272 | 6 | 212 ms |
| latest_long | 0.100 | 0 | 225 ms |
| chirp_3 (streaming) | 0.144 | 1 | 1251 ms |

Mandarin, 10 sentences of `stt_accuracy.py` (Google TTS, real time): CER latest_short 0.061, latest_long 0.061 (identical transcripts), chirp_3 batch 0.085; median final 202 / 209 ms.

## Sim stack (branch overlay, real Google STT, tone TTS; `docs/logs/fix-stt-stream-dropouts-sim-2026-10-11.log`)

Scratch compose project `weixue-sttdrop-sim` on 127.0.0.1:18500, `server/sim/stt.config.yaml` (GoogleSTTStream), `WEIXUE_STT_CAPTURE=1`; the sim device sends the first 2.34 s of the clip at once before the `listen detect` (`--wake-audio`).

- One breath, "Jarvis, what time is it?" at -6 and -30 dBFS: 23/23 correct transcripts (3 trial runs + 20); 10 from latest_short (`stt_final_latency_ms` 162-615), 12 through `fallback=truncated` and 1 through `fallback=stream_error` (the service cancelled the stream after its end of utterance), all chirp_3 at 1400-2923 ms.
- Wake with "Jarvis." only (1.0 s noise, the word, 0.5 s noise), cue, then "Yes.": 6/6 cue played, wake-only segment dropped before STT, "Yes." recognized by latest_short.
- Replay of the 20 sim captures (server-side PCM after Opus, recorded arrival times): latest_short truncated 14/20 (stream-only WER 0.46); with the fallbacks WER 0.0 = chirp_3 batch 0.0; latest_long 0/20 truncated, WER 0.0.
