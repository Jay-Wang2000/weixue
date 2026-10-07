# Latency spike — LLM first token (direct API vs Agent SDK), Google STT and TTS (2026-10-07)

Measured on this Mac, 2026-10-07 ~17:00–17:38 IST, personal API key. Questions: Q1 "What time is it in Dublin?", Q2 "Is the living room light on?". Per (path, model, question): 1 warm-up + 5 measured runs; median and p90 over the 5 (p90 linearly interpolated, so with n=5 it sits between the 4th and 5th values). Times are client-side, from the request call to the event.

- **First output**: first `text_delta`, or the first `tool_use` block start, whichever comes first.
- **First text**: first `text_delta`. When the model calls a tool first, this includes the tool round trip and a second API call.
- **Input / cache read**: tokens per API request (an SDK turn with one tool call makes two requests).

## Results

| Path | Model | Q | First output med / p90 (ms) | First text med / p90 (ms) | Input tok | Cache read tok | Notes |
|---|---|---|---|---|---|---|---|
| A direct, no tools | claude-sonnet-5-5 | Q1 | 892 / 1753 | 892 / 1753 | 40 | 0 | effort low, adaptive thinking (default). 1 of 5 runs used all 60 tokens on thinking and returned no text (n=4 for text) |
| A direct, no tools | claude-sonnet-5-5 | Q2 | **715** / 743 | **715** / 743 | 38 | 0 | effort low, adaptive |
| A′ direct, no tools | claude-sonnet-5-5 | Q1 | 721 / 765 | 721 / 765 | 39 | 0 | `thinking: between_tools` (thinking off), effort low |
| A′ direct, no tools | claude-sonnet-5-5 | Q2 | 837 / 1607 | 837 / 1607 | 37 | 0 | same; one 1975 ms outlier |
| B direct, no tools | claude-fable-5-1 | Q1 | 2224 / 4463 | 2224 / 4463 | 40 | 0 | effort low, `thinking` omitted (always on). One 5.7 s outlier |
| B direct, no tools | claude-fable-5-1 | Q2 | 2452 / 6352 | 2452 / 6352 | 38 | 0 | same. One 8.8 s outlier |
| A-7 direct + 7 voice tools | claude-sonnet-5-5 | Q1 | 793 / 1963 | 793 / 1963 | 8,685 | 8,577 | decomposition run; 7 stub tools ≈ 24 KB (= today's voice path) |
| A-7 direct + 7 voice tools | claude-sonnet-5-5 | Q2 | 836 / 853 | 2222 / 3678 | 8,683 | 8,577 | 1 `ha_get_state` call every run |
| A-77 direct + 77 tools | claude-sonnet-5-5 | Q1 | 2782 / 3848 | 2782 / 3848 | 91,447 | 91,339 | decomposition run; same 77 stub schemas as C, no SDK |
| A-77 direct + 77 tools | claude-sonnet-5-5 | Q2 | 2369 / 2837 | 5040 / 6744 | 91,445 | 91,339 | 1 `ha_get_state` call every run |
| C Agent SDK, warm client | claude-fable-5-1 | Q1 | 4137 / 5240 | 9734 / 18600 | ~94.5k | ~94k | tools called in 3/5 runs (`ha_get_template`, `WebSearch`, `ha_get_entity`) |
| C Agent SDK, warm client | claude-fable-5-1 | Q2 | 4145 / 6417 | 9510 / 12309 | ~95.5k | ~95k | 1 `ha_get_state` call every run |
| C Agent SDK, warm client | claude-sonnet-5-5 | Q1 | 2475 / 4521 | 4349 / 8184 | ~94.5k | ~94k | tools called in 3/5 runs (`ha_get_template`, `ha_search_entity`, `ha_search_state`) |
| C Agent SDK, warm client | claude-sonnet-5-5 | Q2 | 2431 / 3792 | 5330 / 6302 | ~96k | ~96k | 1 `ha_get_state` call every run |
| C0 SDK cold start | claude-fable-5-1 | — | 926 ms | — | — | — | `ClaudeSDKClient(...)` + `connect()` (CLI spawn → ready, stub MCP connected) |
| C0 SDK cold start | claude-sonnet-5-5 | — | 846 ms | — | — | — | same |

First SDK request of a fresh prefix (cache write ~93k tokens) took 4.6 s to the first token on Fable 5.1 (warm-up, excluded above).

## Setup

- **HA MCP**: `HA_MCP_URL` (LAN address) was unreachable from the host (connect refused within 3 ms; HA is reachable only through the mcp-bridge container's WireGuard tunnel). Path C therefore used a **local stdio stub MCP server**: 77 dummy `ha_*` tools, 264 KB of `tools/list` JSON (ha-mcp 8.6.0 lists 77 tools, 270 KB, per `docs/ha-mcp.md`). `tools/call` returns a fixed JSON state (`light.living_room_ceiling_lights` = off). The real endpoint is HTTP and its schemas differ in content, so its token count will differ from ~91k.
- **Path C options**: `ClaudeSDKClient` with `include_partial_messages=True`, one client per model reused across all 12 turns (one SDK session, so history grows ~0.5k tokens per turn), `effort="low"`, `system_prompt` = the same one-sentence string as A/B, `tools=["WebSearch"]` (no other built-ins), `allowed_tools=["WebSearch","mcp__ha"]`, `permission_mode="dontAsk"`, `strict_mcp_config=True`, `setting_sources=[]`, an isolated `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `max_turns=3`. The init message reported 78 tools (77 MCP + WebSearch): the CLI loaded all MCP schemas up front, with no deferred tool search.
- **max_tokens**: 60 for A/B. For C and the A-7/A-77 runs it was 300: at 60 the SDK turn failed ("response exceeded the 60 output token maximum") once tool calls were in play.
- **Fable 5.1 settings**: `thinking` omitted (only `adaptive` is accepted; `disabled` and `budget_tokens` return a 400). Effort via `output_config.effort="low"`. No sampling params. With the default `display: "omitted"` the first stream event arrived only when text started (TTFB = first text). No server-side `fallbacks` were sent in this spike; production Fable code should send them.
- **Sonnet 5.5 settings**: adaptive (default) or `between_tools`, effort low. `disabled` would return a 400.
- **Not touched**: the live Docker stack and the device. No audio.
- **Spend**: about $3.3 in total: SDK Fable session $1.95 (of which $1.16 was the first-turn cache write), SDK Sonnet $0.49, SDK smoke run $0.29, A-77 ≈ $0.45, everything else < $0.10. Warm per-turn cost: Fable ≈ $0.03 (no tool) / $0.056 (one tool call); Sonnet ≈ $0.02 / $0.04.

## Versions

- Python 3.13.11 (venv `~/Projects/weixue-vendor/spikes/latency/.venv`), macOS 26.6.2
- `anthropic` 1.11.0 (`httpx2` 2.13.1)
- `claude-agent-sdk` 0.2.164, bundled Claude Code CLI 2.1.292
- `mcp` 2.3.0 (installed but unused; the stub speaks JSON-RPC over stdio directly)
- `google-cloud-speech` 2.40.0, `google-cloud-texttospeech` 2.37.0 (the `server/requirements-overlay.txt` pins), `google-api-core` 2.41.0, `google-auth` 2.60.0, `grpcio` 1.84.0
- Scripts: `bench_direct.py` (A/A′/B), `bench_direct_tools.py` (A-7/A-77), `bench_sdk.py` (C/C0), `stub_mcp.py`, `bench_google.py` (D/E). Raw rows are in `results_direct.jsonl`, `results_sdk.jsonl` and `results_google.jsonl` in the same directory.

## Conclusion (budget: agent text first-token ≤ 800 ms median; direct path as the floor)

1. Floor: with no tools, direct Sonnet 5.5 reaches the first token in 0.72–0.89 s median (both thinking modes), right at the 800 ms budget. Direct Fable 5.1 takes 2.2–2.5 s with thinking it cannot turn off, so Fable misses the budget on every path.
2. The Agent SDK path misses by 2–5×. First output takes 2.4–2.5 s on Sonnet and 4.1 s on Fable. First text takes 4.3–5.3 s (Sonnet) and 9.5–9.7 s (Fable), because the agent also calls tools for Q1 in 3/5 runs.
3. Most of that cost is the tool surface, not the SDK harness. The direct API with the same 77 schemas (91k tokens, cache hit) needs 2.4–2.8 s, about the same as the SDK. The 7 voice-path tools (8.7k tokens) need 0.79–0.84 s. Cold start is 0.85–0.93 s, paid once per client.
4. Per the brief's red line this triggers the hybrid path. Quick asks go through direct streaming Sonnet 5.5 with the small voice tool set (≈ 0.8 s to the first output, ≈ 2.2 s to the first text after one HA read). Tasks go through the SDK, where Fable 5.1 at 4–10 s is acceptable for long work only.
5. Open: (a) re-run C against the real ha-mcp endpoint from the home LAN or the bridge network, since its token count may not match the stub's ~91k; (b) test SDK tool search/deferred MCP loading, which might cut the 91k-token prefix; (c) measure end-to-end speech-end → first TTS audio (budget ≤ 1.5 s) on the hybrid quick path.

## STT and TTS (D, E)

Same Mac, 2026-10-07 ~17:50 IST, 1 warm-up + 5 runs, one client per service reused across runs (the providers also keep a single client). Credentials: the `GOOGLE_SA_FILE` service account.

- **Clip**: "Is the living room light on?", synthesized with Chirp 3 HD (Aoede) at 16 kHz LINEAR16, padded to 2.00 s (speech 0.39–1.61 s). All 30 STT runs returned the exact text.
- **D-batch**: `Recognize`, as `google_stt.py` does it: v2, `us-speech.googleapis.com`, recognizer `_`, `chirp_3`, `language_codes ["en-US","cmn-Hans-CN"]`, automatic punctuation, LINEAR16 16 kHz. The timer runs from the call to the response; the whole 2 s clip is uploaded at once.
- **D-stream**: `StreamingRecognize` with the same `RecognitionConfig` plus `interim_results=True`. Audio is sent in real time in 100 ms chunks, then the request stream is half-closed. "Final" is measured from the last chunk sent, which is when the server's VAD stop would trigger the batch call.
- **E**: `streaming_synthesize`, as `google_tts.py` does it: `en-US-Chirp3-HD-Charon`, PCM at 24 kHz, speaking_rate 1.0, a config request then one text request, first-result prefetch off. The sentence is "The living room light is off, and the kitchen light is on." (12 words). The first audio chunk was ≥ one 60 ms Opus frame in every run, so first chunk = first frame.

| Path | Metric | Median (ms) | p90 (ms) | Notes |
|---|---|---|---|---|
| D-batch | Recognize latency | 1427 | 1597 | runs 934–1643 |
| D-stream (en-US + cmn-Hans-CN) | final after last audio chunk | **767** | 1392 | 0 interim results in every run |
| D-stream (en-US only) | final after last audio chunk | 853 | 1049 | also 0 interim results |
| D-stream | first interim | — | — | `chirp_3` returned no interim results with `interim_results=True` in either language config. The final arrived only after the half-close, never through endpointing while audio was still flowing |
| E | first audio chunk | **195** | 263 | runs 184–303 |
| E | stream complete | 642 | 660 | 3.1–3.8 s of audio per run |

Google spend: 30 STT requests × 2 s plus about 400 TTS characters, under $0.05 at list prices.

## End-to-end budget

Sum = STT (D median) + LLM time to first text on Q2 (median from the tables above) + TTS first audio (E median, 195 ms). The budget is speech end → first audio ≤ 1.5 s median.

| LLM path (Q2 "Is the living room light on?") | LLM first text | With batch STT (1427) | With streaming STT (767) | Streaming STT + wait for first sentence |
|---|---|---|---|---|
| A direct Sonnet 5.5, no tools (cannot answer; floor) | 715 | 2337 | **1677** | 2547 |
| A′ direct Sonnet 5.5, thinking off | 837 | 2459 | 1799 | 2726 |
| B direct Fable 5.1, no tools | 2452 | 4074 | 3414 | 4824 |
| A-7 direct Sonnet 5.5 + 7 voice tools, 1 HA read | 2222 | 3844 | 3184 | 3198 |
| A-77 direct Sonnet 5.5 + 77 tools, 1 HA read | 5040 | 6662 | 6002 | 6038 |
| C Agent SDK Sonnet 5.5 + 77 tools | 5330 | 6952 | 6292 | 6295 |
| C Agent SDK Fable 5.1 + 77 tools | 9510 | 11132 | 10472 | 10484 |

The last column uses the LLM's full reply time instead of its first token. `google_tts.py` sends text to TTS only at an English sentence end followed by whitespace, or at the end of the turn, and every reply here was a single sentence, so first audio waits for the whole reply.

Not in the sums:
- the VAD's 200 ms `min_silence_duration_ms` (upstream default, not overridden) before the server declares speech end;
- device ↔ server audio transport and Opus playback buffering;
- the real ha-mcp read (99–192 ms per call through the bridge, `docs/ha-mcp.md`; the stub answers in ~0 ms).

Every row would therefore be at least ~0.2–0.4 s slower on the device.

Findings:
1. **No combination meets ≤ 1.5 s at the median.** The closest is streaming STT + direct Sonnet with no tools + TTS at 1.68 s (0.18 s over, before VAD and transport), and that path cannot read HA. A real home question needs one HA read: 3.2 s with the 7 voice tools and streaming STT, 3.8 s with batch STT. The SDK paths take 6.3–11 s.
2. **Streaming STT saves 660 ms at the median** (1427 → 767) and ~205 ms at p90 (1597 → 1392) on a 2 s clip. The gain comes from uploading and decoding while the user speaks, not from early finals: `chirp_3` returned no interim results, and its final arrived only after the stream closed. The server's VAD therefore still decides speech end.
3. **TTS is not the bottleneck.** First audio arrives in 0.2 s. On one-sentence replies the real cost is the wait for the sentence end (0.9 s on path A).
4. **The largest remaining cost is the second LLM round trip after an HA read** (A-7: 0.84 s to the tool call, 2.2 s to first text). Options that would close the gap, none decided:
   - speak a short acknowledgement while the tool runs;
   - start TTS at the first clause instead of the sentence end;
   - prompt for a short first sentence;
   - a closer STT region than `us` (an external-service decision for Jay).

## Part 2: STT and TTS

Measured on this Mac, 2026-10-07, service-account credentials from `server/.env`. Per config: 1 warm-up + 5 measured runs, median and p90 as in part 1. Clips: Google TTS (`en-US-Chirp3-HD-Charon`, 16 kHz mono LINEAR16) of "What time is it in Dublin?" (**1.4 s**, `work/q1_dublin_16k.wav`) and, for scaling, "What time is it in Dublin, and is the living room light on right now?" (**4.2 s**, `work/q_long_16k.wav`). Config as in `google_stt.py`: explicit LINEAR16 decoding, punctuation on, `language_codes` = en-US + cmn-Hans-CN unless noted, recognizer `_`, endpoint `us-speech.googleapis.com` (chirp_2 only exists in `us-central1`).

- **Batch**: `recognize` call → response (warm client).
- **Streaming**: `streaming_recognize`, `interim_results=True`, 100 ms chunks fed in real time. "Final after end-of-audio" = last `is_final` result minus the instant the last audio chunk was sent (the half-close, i.e. the VAD-stop moment on the device). A second pass with 700 ms of trailing silence fed in real time gives the same value once the 700 ms is subtracted (e.g. chirp_3 en-US 1384 − 700 ≈ 684 ms).
- **TTS**: `streaming_synthesize` as in `google_tts.py` (PCM 24 kHz, `en-US-Chirp3-HD-Charon`, rate 1.0, first-response prefetch disabled, warm client). Time from the call to the first non-empty `audio_content`. Sentence: "The living room light is on and the temperature is twenty degrees." (12 words, 3.7 s of audio).

### (D) STT, ms, median / p90

| Model (location) | Langs | Clip | Batch | Stream: first interim | Stream: final after end-of-audio |
|---|---|---|---|---|---|
| chirp_3 (us) | en-US + cmn-Hans-CN | 1.4 s | 1404 / 1415 | none | **843 / 1179** |
| chirp_3 (us) | en-US | 1.4 s | 1407 / 1441 | none | **683 / 969** |
| chirp_3 (us) | en-US + cmn-Hans-CN | 4.2 s | 1802 / 1854 | none | **896 / 1056** |
| latest_short (us) | en-US | 1.4 s | 1130 / 1254 | 894 / 1087 | **228 / 424** |
| latest_short (us) | en-US + cmn-Hans-CN | 1.4 s | 1299 / 1483 | 1743 / 1832 | **336 / 433** |
| latest_short (us) | en-US | 4.2 s | 1663 / 1813 | 1137 / 1414 | **274 / 385** |
| chirp_2 (us-central1) | en-US | 1.4 s | 1581 / 1757 | none | 1238 / 1564 |

- chirp_3 accepts `streaming_recognize` in `us` with the production language pair, but returns **no interim results** (the flag is accepted, only final results arrive). chirp_2 behaves the same. Only `latest_short` emitted interims.
- All configs transcribed the clip correctly ("What time is it in Dublin?"; the long clip too).
- Batch latency grows with clip length (chirp_3 1.40 s → 1.80 s for 1.4 s → 4.2 s). The streaming tail after end-of-audio does not (chirp_3 ~0.85–0.9 s, latest_short ~0.23–0.34 s).
- Streaming saves versus batch on the same model and clip: chirp_3 0.56–0.72 s (1.4 s clip), 0.91 s (4.2 s clip); latest_short 0.90 s (1.4 s), 1.39 s (4.2 s).
- `latest_short` with the production language pair accepted both batch and streaming; Mandarin accuracy was not tested.

### (E) TTS Chirp 3 HD streaming first audio, ms

| Voice | Runs | First audio med / p90 | Raw |
|---|---|---|---|
| en-US-Chirp3-HD-Charon | 5 | **327 / 411** | 321, 459, 339, 313, 327 |

Total synthesis time for the 3.7 s of audio was 680–912 ms.

### End-to-end budget (speech-end → first audio, medians, ms)

Sum of medians: STT final after end-of-audio + LLM first output (part 1: direct Sonnet 7 tools 0.8 s, Agent SDK Sonnet 2.4 s, Agent SDK Fable 4.1 s) + TTS first audio 0.33 s. STT column uses the 1.4 s clip (4.2 s clip in brackets where it changes the verdict). Excludes device→server transport, VAD silence timeout, Opus encode and playback start.

| STT | STT | + direct Sonnet 0.8 | + SDK Sonnet 2.4 | + SDK Fable 4.1 |
|---|---|---|---|---|
| Batch chirp_3 (current provider) | 1404 | 2531 | 4131 | 5831 |
| Batch latest_short | 1130 | 2257 | 3857 | 5557 |
| Stream chirp_3 | 843 | 1970 | 3570 | 5270 |
| Stream latest_short | 228 (274) | **1355 (1401)** | 2955 | 4655 |

Budget ≤ 1.5 s met only by **streaming `latest_short` + direct Sonnet** (1.36 s; 1.40 s with the 4.2 s clip). It has 0.14–0.15 s of headroom, and its p90 components (0.42 + 1.96 + 0.41 s) exceed the budget.

### Conclusion

1. The ≤ 1.5 s speech-end → first audio budget is met by one combination: streaming `latest_short` (≈ 0.23–0.34 s after end-of-audio) + direct Sonnet 5.5 with the voice tool set (0.8 s) + Chirp 3 HD streaming TTS (0.33 s) = 1.36 s median, with almost no headroom. Every Agent SDK combination misses (3.0 s Sonnet, 4.7 s Fable), and so does every batch-STT combination (≥ 2.26 s).
2. Streaming STT saves 0.9–1.4 s on `latest_short` and 0.6–0.9 s on chirp_3 versus batch on the same model. chirp_3 and chirp_2 stream but emit no interims and finalize 0.7–1.2 s after end-of-audio, so only `latest_short` brings STT under 0.35 s.
3. TTS is not the bottleneck (0.33 s median first audio); the LLM first output is. Sentence segmentation in `google_tts.py` waits for a sentence end before the first request, which these numbers do not include. Adopting `latest_short` streaming needs a streaming ASR provider interface (the current provider is `NON_STREAM`).

### Part 2 setup

- Scripts: `bench_speech.py` in the spike directory (raw rows in `results_speech.jsonl`). `google-cloud-speech` 2.41.0 and `google-cloud-texttospeech` 2.38.0 installed into the spike venv.
- Not touched: Docker stack, device, audio output. Spend: tiny clips only, a few minutes of audio in total (estimated well under $0.20, not billing-verified).
