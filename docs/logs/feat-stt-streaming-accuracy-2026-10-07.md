# STT accuracy check: latest_short streaming vs chirp_3 batch (2026-10-07)

Decision 42 gate: 10 fixed Mandarin sentences (and 10 English) on `latest_short` streaming, compared with today's `chirp_3` batch. Run on this Mac, 2026-10-07 ~18:15 IST, `GOOGLE_SA_FILE` service account, endpoint `us-speech.googleapis.com`, recognizer `_`, `language_codes ["en-US", "cmn-Hans-CN"]`, automatic punctuation on, LINEAR16 16 kHz mono.

- **Script**: `server/scripts/stt_accuracy.py`. Clips and raw rows (`results.jsonl`) in `~/Projects/weixue-vendor/spikes/stt-accuracy/`.
- **Clips**: Google TTS at 16 kHz, Chirp 3 HD voices alternating Charon / Aoede (`en-US-…`, `cmn-CN-…`), 1.1–3.0 s each.
- **Streaming**: 0.3 s of silence, the clip, then 0.2 s of silence (the VAD's `min_silence_duration_ms`), fed in real time as 60 ms frames (the device frame size). The stream is half-closed after the last frame. Latency = last final result minus half-close.
- **Batch**: `Recognize` on the bare clip. Latency = call to response, warm client.
- **Scoring**: English WER on lower-case words, Mandarin CER on characters. Punctuation and spaces are ignored, digits become number words or 汉字 numerals, `%` becomes "percent" and `°` becomes "degrees". Error rate = total edits / total reference tokens.

## Results

| Model | Language | Error rate | Exact | Confidence min / median | Final after half-close med / max (ms) | Batch latency med (ms) |
|---|---|---|---|---|---|---|
| latest_short (stream) | en WER | 0.0% | 10/10 | 0.974 / 0.986 | 220 / 314 | — |
| latest_short (stream) | zh CER | 0.0% | 10/10 | 0.910 / 0.910 | 300 / 396 | — |
| chirp_3 (batch) | en WER | 0.0% | 10/10 | — | — | 1355 |
| chirp_3 (batch) | zh CER | 0.0% | 10/10 | — | — | 1451 |

Mandarin set: 打开客厅的灯 · 把卧室的空调调到二十六度 · 现在几点了 · 明天都柏林会下雨吗 · 关掉厨房的灯 · 客厅的灯开着吗 · 帮我设一个十分钟的定时器 · 把音量调小一点 · 今天的天气怎么样 · 晚上十点提醒我关窗户. English set: in the script (`ENGLISH`).

- Both models transcribed every sentence. They differ only in punctuation: latest_short wrote "Is the kitchen light on." and "Dim the hallway, lights to 50%."; chirp_3 puts spaces between Han characters, which the providers remove.
- Both models write numbers as digits ("26度", "10分钟", "10点", "20°", "50%", "6:00").
- Every final carried the detected language: `en-US`, and `cmn-hans-cn` (lower case) for Mandarin. latest_short sent `END_OF_SINGLE_UTTERANCE` on all 10 Mandarin clips and on none of the English ones.
- **latest_short confidence is 0.910 on every Mandarin final.** English confidence varies (0.974–0.997).

An earlier pass the same evening, before `°` was normalized, gave the same transcripts: English 1.7% WER on both models from "20°", Mandarin 0.0% CER, Mandarin streaming final 258 / 508 ms.

## Harder Mandarin probes (scratch, not part of the fixed set)

Same configuration, one run each:

| Clip | latest_short (confidence) | chirp_3 |
|---|---|---|
| 把卧室的空调调到二十六度, white noise SNR 10 / 3 / 0 dB | correct ×3 (0.91) | correct ×3 |
| 帮我设一个十分钟的定时器 at speaking rate 1.6 | correct (0.91) | correct |
| 帮我打开 living room 的灯 (code-switched) | 帮我打开**李炳荣**的灯 (0.91) | 帮我打开 living room 的灯 |
| 把加湿器的雾量调到三档 | 把加湿器的**物**量调到三档 (0.91) | correct |
| 嗯那个就是把那个灯稍微调暗一点吧 | correct, keeps 嗯 (0.91) | correct, drops 嗯 |
| English temperature sentence, SNR 3 / 0 dB | correct (0.973 / 0.972) | correct |

## Conclusion

1. On the fixed set, latest_short Mandarin accuracy equals chirp_3 (0.0% CER for both), so Mandarin does not degrade. Per decision 42, latest_short streaming serves both languages.
2. Mandarin finals report confidence 0.91 whether right or wrong, so confidence cannot single out bad Chinese finals. `zh_fallback_confidence` is set to **0.9**: no Mandarin final is re-recognized, and the setting stays as a switch. Any value above 0.91 re-recognizes every Chinese utterance with chirp_3 batch, which costs about 1.4 s (batch median above).
3. Streaming finals arrive 0.22 s (English) and 0.30 s (Mandarin) after the half-close, at the median. chirp_3 batch takes 1.36–1.45 s.
4. The probes are out of scope for the gate. chirp_3 did better on code-switched English inside Mandarin and on one rarer word (雾量). The provider has no signal that would route those utterances to chirp_3.
5. Caveat: clean synthesized speech, not microphone audio. The milestone end-to-end session checks accuracy on the device.
