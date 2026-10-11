# Backend server

xiaozhi-esp32-server 0.9.7 in Docker Compose on the Mac (Rancher Desktop). The server image is built from the upstream clone at `${WEIXUE_VENDOR:-~/Projects/weixue-vendor}/xiaozhi-esp32-server`, pinned in `vendor.lock`; `setup.sh` refuses to build if the clone is not at that commit or has local changes. The resulting image carries the commit in its `org.opencontainers.image.revision` label. Other images are pinned by digest in `vendor.lock`, `docker-compose.yml` and `.env.example`.

## Firmware endpoints

Replace `<LAN_IP>` with `LAN_IP` from `server/.env`.

| Purpose | URL |
|---|---|
| OTA (firmware `OTA_URL`) | `http://<LAN_IP>:8003/xiaozhi/ota/` |
| WebSocket (returned by OTA) | `ws://<LAN_IP>:8000/xiaozhi/v1/` |
| Vision explain | `http://<LAN_IP>:8003/mcp/vision/explain` |
| Proactive webhook and device long-poll | `http://<LAN_IP>:8003/weixue/proactive/...` (`docs/proactive.md`) |

`GET` on the OTA URL returns a plain-text banner. The device `POST`s JSON with `Device-Id` and `Client-Id` headers and receives the *OTA response*: `{"server_time":…,"firmware":…,"websocket":{"url":"ws://<LAN_IP>:8000/xiaozhi/v1/","token":""}}`. `server_time` is `{"timestamp": <UTC ms>, "timezone_offset": <minutes>, "tz": "<POSIX TZ>"}`; `tz` (from `weixue_clock`) is the POSIX rule of `TZ` in `server/.env` (for example `Europe/Dublin` → `IST-1GMT0,M10.5.0,M3.5.0/1`), read from the footer of its TZif file, and is omitted when `TZ` is unset or has no TZif v2+ file. Compose passes the `.env` value as `WEIXUE_TZ` (`${TZ:-}`), because the container's own `TZ` falls back to `UTC` and an explicit `TZ=UTC` must stay distinguishable from that fallback. The firmware keeps a UTC clock with this TZ (`docs/firmware.md` Device behaviour); `timezone_offset` (`TZ_OFFSET`) is used only without `tz`.

## Network boundary

- OTA (8003) and WebSocket (8000) are published on `BIND_IP` from `server/.env`. `setup.sh` accepts `0.0.0.0` or `127.0.0.1`, and on Linux also an IPv4 address on a local interface. The Mac uses `BIND_IP=0.0.0.0`: both ports on every Mac interface, the same exposure as an unbound `8000:8000` mapping. `mcp-endpoint-server` (8004) is on `127.0.0.1` only.
- `server.auth.enabled: true`; `allowed_devices` is rendered from `ALLOWED_DEVICES` in `server/.env` (lower-case MACs, comma-separated; `.env.example` holds the 1.75C, `7c:0c:5f:42:4b:fc`). `setup.sh` fails at pre-flight when it is unset or empty and at validation when it is not lower-case.
- Strict allow-list (`weixue_patches.py`): an OTA `POST` whose `Device-Id` is listed gets the websocket section with `"token":""`; any other id, or none, gets `403` `{"success":false,"message":"device not allowed"}` and no websocket section. A WebSocket handshake with an unlisted `Device-Id` (header or query parameter) receives `认证失败` and is closed, whatever `Authorization` token it presents; a listed id connects with or without one. Both refusals log a warning naming the id. The firmware retries a failed OTA check with 10 s doubling back-off, at most 10 times, then uses its stored websocket settings.
- A WebSocket client without `Device-Id` receives the upstream "端口正常…" banner and is closed (the readiness probe).
- The list admits a claimed header value: it is not proof of device identity, and OTA/WS are plain HTTP/WS.
- Enrol a device: read its id from the refusal warning (`docker logs xiaozhi-esp32-server 2>&1 | grep 'not in allowed_devices'`) or its serial log, append it to `ALLOWED_DEVICES` in `server/.env`, run `./scripts/setup.sh`, then reboot the device so it repeats the OTA check.

## MCP endpoint (`mcp_endpoint`)

Service `mcp-endpoint-server`, published on `127.0.0.1:8004` only. Format:

```
ws://<host>:8004/mcp_endpoint/mcp/?token=<AES-encrypted token>
```

- The token is derived from a key generated on first start (`server/data/mcp-endpoint/.mcp-endpoint-server.cfg`, mode 0600) and printed in `docker logs mcp-endpoint-server`. The full URL is a secret; redact container logs before sharing them.
- The server container uses `ws://mcp-endpoint-server:8004/...` (written into `server/data/.config.yaml` by `setup.sh`).
- MCP clients on the Mac use `ws://localhost:8004/...` with the same token.
- Deleting `server/data/mcp-endpoint/` regenerates the key and invalidates every issued URL.
- `setup.sh` also writes the tool-side URL to `server/data/mcp-bridge/endpoint-url` (0600) for the `mcp-bridge` service, which connects ha-mcp to the endpoint: [ha-mcp.md](ha-mcp.md).

## Adding provider code

`server/overlay/` is the only way to add or change server code. Its contents mirror `main/xiaozhi-server/` of the upstream clone (for example `server/overlay/core/providers/llm/<name>/<name>.py`) and are copied over the upstream tree at image build (`server/Dockerfile`, `__pycache__` excluded from both). Python packages the overlay needs are listed in `server/requirements-overlay.txt`; `server/requirements-overlay.lock` pins them and their transitive dependencies (regeneration command in `scripts/lock_overlay_deps.py`). The Dockerfile installs the lock with `--no-deps` and fails the build if `pip check` finds a conflict. Apply with `./scripts/setup.sh`. The vendored clone is never edited.

Changes to upstream functions that are too small to justify overriding a whole module go in `server/overlay/weixue_patches.py` (and `weixue_clock.py` for the OTA clock, `weixue_device_tools.py` for device tool classes). The image's entrypoint, `weixue_main.py`, applies them and then runs upstream `app.py`. Current patches:

- `ConnectionHandler._extract_direct_answer_response` is replaced by `decode_direct_answer`: it decodes the `response` JSON string of the streamed `direct_answer` arguments (escapes, `\uXXXX`, surrogate pairs) and stops at its closing quote or at an incomplete escape, so each streamed result extends the previous one. `_clean_response_garbage` returns its input unchanged: with framing removed by the decoder, no chunk-level stripping is applied (upstream's stripped whitespace and quotes at chunk edges: "isfour", "Say 'onnow").
- `ConnectionHandler.handle_connection`, `ListenTextMessageHandler.handle` and the aiohttp app of `core.http_server` are wrapped for proactive messages (`docs/proactive.md`).
- `ListenTextMessageHandler.handle` is also wrapped for the wake cue, and `core.providers.vad.silero.VADProvider.is_vad` for the wake utterance hold (see Wake cue).
- `OTAHandler.handle_post` and `WebSocketServer._handle_auth` are wrapped for the strict device allow-list (see Network boundary).
- `ConnectionHandler.just_woken_up` is a property that always reads `False`: upstream sets it on every `listen detect` and then drops the next 2 s of microphone audio.
- `receiveAudioHandle.no_voice_close_connect`: an audio frame received while the server holds the turn (`client_is_speaking`, from the STT result or wake cue to `tts stop`) refreshes `last_activity_time`, so the idle close counts from the end of the reply (see Conversation timing).
- `McpTextMessageHandler.handle` logs every device MCP notification whose method starts with `notifications/weixue/` as `device event <method> device=<mac> params=<json>` before upstream handles it (panel decisions, `docs/firmware.md` Monitor panel; IMU gestures).
- `OTAHandler.handle_post` (`weixue_clock.py`) adds `server_time.tz` to the OTA response; the server logs `OTA server_time.tz: <tz>` at start.
- `intentHandler.checkWakeupWords` is replaced: a transcript that is only a `wakeup_words` entry (case-insensitive, punctuation ignored) is dropped with no reply. `enable_wakeup_words_response_cache` and the cached `wakeup_words` clips are therefore unused.
- `sendAudioMessage` (in `core.handle.sendAudioHandle` and `core.providers.tts.base`) is wrapped to record, for the current sentence, the audio packets handed over, whether a text segment got no audio and whether a send raised or was cancelled, and to keep each captioned reply sentence (echo guard). `sendAudioHandle._do_send_audio`, the write of one packet to the device, is wrapped to count the packets actually written and to time each caption's first and latest written packet. A sentence counts as complete when its LAST returned with every handed packet written, no send error, and the sentence still current and unaborted (proactive delivery outcome, `docs/proactive.md`). The Google TTS provider reports a synthesis that raised to the same record.
- `handleAbortMessage` (as imported by `core.handle.abortHandle`, `receiveAudioHandle` and `abortMessageHandler`) is wrapped to count every abort as a connection event for the wake cue.
- `core.providers.asr.base.startToChat` is wrapped as the barge-in gate and echo guard (see Barge-in, Echo guard); a transcript that passes it is a connection event and cancels a pending wake cue.
- `SimpleHttpServer.__init__` is wrapped so proactive device requests use its `server.auth` (`docs/proactive.md`, Device identity).
- `MCPClient.add_tool` and `call_mcp_tool` (in `device_mcp.mcp_handler`, `mcp_executor` and the package) are wrapped (`weixue_device_tools.py`): only agent-visible device tools enter the model's function list or are called on the device (`docs/agent-protocol.md`, Device tools).
- `ConnectionHandler.handle_connection` also keeps the connection findable by its `session_id` while it runs (`session_connection`, used by the `claude_agent` provider) and closes the connection's open agent-service stream when the session ends; the `handleAbortMessage` wrapper closes that stream on every abort.
- `ASRProviderBase.handle_voice_stop` is wrapped to keep the VAD's end of utterance as the turn's speech end (`turn_latency`, below); the playback record adds each captioned segment of a `claude_agent` turn to that turn's metrics. The same wrapper is the segment probe (see ASR segment probe and capture) and drops a wake-only segment before STT (see Wake cue).
- `ASRProviderBase.receive_audio` and `speech_to_text_wrapper` are wrapped for the segment probe (frame arrival times; the provider's last transcript), `ConnectionHandler._route_message` counts the audio messages of the connection, and `ListenTextMessageHandler.handle` counts `listen detect` / `listen start` and logs `listen start without a wake detect: mode=<m> start=<n> frames_before=<n>`. At the end of every session the server logs `audio session end packets=<audio messages> frames=<frames handed to ASR> bytes=<n> segments=<n> detects=<n> listen_starts=<n>`.

## Conversation timing (ADR-7)

| Key | Value | Effect |
|---|---|---|
| `barge_in` | `VOICE_BARGE_IN` (default 0 → false) | See Barge-in |
| `close_connection_no_voice_time` | 8 | Seconds without user voice after the reply or wake cue ends (or after the wake event) before the server closes the WebSocket; the device returns to idle on close. The connection-level timeout is this value + 60 s |
| `end_prompt.enable` | false | The idle close sends no farewell turn |
| `wake_cues` | 3 clips | See Wake cue; with an empty list a wake is handled by upstream |
| `enable_greeting` | false | Without `wake_cues`, a wake gets `stt` + `tts start/stop` and no audio |
| VAD `SileroVAD.min_silence_duration_ms` | 200 (upstream) | End-of-utterance silence; not overridden |

The device listens in realtime mode and streams microphone audio for the whole session, so the server's VAD sees the follow-up window; a follow-up inside it needs no wake word. Wake-word detection is off while the device listens (`CONFIG_WAKE_WORD_DETECTION_IN_LISTENING` unset).

### Wake cue

`wake_cues` lists `{file, text}` clips: `server/overlay/config/assets/wake_cues/{hmm,mm-hm,yes}.wav` ("Hmm?", "Mm-hm?", "Yes?"), rendered once with the live Google TTS voice (en-US Chirp 3 HD, `speaker` of `TTS.GoogleTTS`), trimmed to 0.46-0.60 s, 24 kHz mono. On a `listen detect` whose text is a wake word:

- One clip is picked at random. 0.6 s after the detect it plays as `tts start`, then (0.2 s later, after the device's decoder reset on entering Speaking) `sentence_start` with the clip text plus its audio, then `tts stop`. The caption shows the clip text.
- The cue is skipped when the VAD heard voice in live audio (the user went on speaking: "Jarvis, what is the weather like in London today?"). Live audio starts 4 frames (0.24 s) after the frames received before the wake's `listen start` (frames the VAD has seen plus those still queued for ASR at the start), so the wake-word burst counts as wake audio however long ASR takes to drain it.
- Neither the detect nor the following `listen start` resets the audio state, so the wake-word audio the device sends before the detect (`CONFIG_SEND_WAKE_WORD_DATA`, 1.98 s ending at detection) reaches VAD and ASR together with the live audio after it. For 0.8 s after that `listen start` the VAD cannot end the utterance. A transcript that is only the wake word is dropped.
- The first segment after the detect with no live voice holds only the wake word the device already reported: it is dropped before STT (`stt_segment kind=wake_only`, `wake-only segment dropped before STT`), so a misheard wake word ("charters", "Chavez") never becomes a turn and the follow-up ("Yes." after the cue) is recognized without waiting for it.
- The microphone is never muted; speech during the cue reaches ASR.
- The cue belongs to the turn that was current at the detect (`sentence_id`) and to the connection's event generation at the detect. Events are an abort (from the device or a barge-in), a wake detect, a transcript that starts a turn and the end of the session; upstream keeps `sentence_id` across an abort, so the generation is what an abort changes. Before the cue takes the connection, and after every await (clip load, `tts start`, decoder reset, audio), it stops once a newer turn owns the connection, an event happened, live voice was heard, the server holds a turn or the connection closed. The cue clears `client_abort` only when no event happened since its detect, so an abort that arrives while it waits stays in force.
- A new detect cancels a pending cue and clears the speaking state that cue set; a transcript that starts a turn and the end of the session cancel it too. A cue that sent `tts start` and fails while it still owns the connection sends `tts stop` and clears the speaking state.

The device's microphone stream is continuous from the wake word into listening (`docs/firmware.md` Device behaviour, wake capture): the audio after the wake word arrives right behind the wake-word audio, before the `listen detect`, so a short request said in one breath with the wake word ("Jarvis, what time is it?") reaches ASR whole and the cue is skipped (`docs/logs/fix-wave3-firmware-wake-trials-2026-10-07.log`).

Re-render the clips with `server/scripts/render_wake_cues.py` (usage in its docstring; runs in the server container).

### Barge-in

`barge_in` in the config, rendered from `VOICE_BARGE_IN` in `server/.env` (`0` → `false`, the default; `1` → `true`; anything else fails `render_config.py`). It decides what speech during playback does. Playback is the time the server holds a turn other than a wake cue (`client_is_speaking`): from a reply's `tts start`, sent with the transcript (so the thinking phase counts), or an announcement's `tts start`, until its `tts stop` or an abort. An utterance belongs to playback when its first voiced frame (the VAD onset) came during playback and no abort or wake detect happened since.

- `barge_in: true`: upstream behaviour. The transcript of such an utterance, unless the echo guard drops it, aborts the playing turn (`handleAbortMessage`, which also closes a `claude_agent` stream) and starts a new turn.
- `barge_in: false`: the utterance is transcribed and then dropped: no abort, no `stt` message, no turn, and no spoken STT reply (the too-long and repeat lines). The log line is `barge-in disabled: dropped utterance during playback: '<text>'`, with ` (echo of '<sentence>')` when the echo guard matches. The playing reply continues, and the normal no-voice window starts at its `tts stop`; an utterance that starts after it is a normal turn. The echo guard only logs (`playback echo kept (barge-in disabled): ...`) for utterances outside playback.
- Either value: speech that starts during a wake cue is the user's request and is handled as today (it aborts the cue). Double-tap, BOOT and an `abort` from the device stop playback as before, and speech after them is a normal turn. The firmware detects no wake word while it speaks (realtime listening), so "Jarvis" during playback is speech like any other. Both `VOICE_LLM` paths behave the same: the gate runs before the LLM.
- The flag is shown as `barge_in=0|1` in the `claude_agent` `turn_latency` line.

### Echo guard

With `barge_in: true`, a transcript is dropped before upstream `startToChat` (so it neither barges in nor reaches the LLM) when its utterance started while a reply sentence was playing and it matches that sentence. Wake cues are never compared: "Yes." after the "Yes?" cue is the user's answer, and an echo of a cue reaches the LLM. "Playing" is each caption's own interval: from the first audio packet of that caption written to the device until `ECHO_TAIL_S` (1 s) after its latest one, so an earlier sentence of the same reply stops being a candidate once its audio is written out. The start is the VAD's first voiced frame of the utterance. There is no acoustic or AEC evidence on this path (the device does its own AEC; `client_aec` is off).

- Fewer than 6 letters: the transcript must match a whole sentence (letter similarity at least 0.8). A short answer that is not the played sentence ("Stop." during a reply) is kept.
- Longer: every word of the transcript (Chinese: character), other than fillers ("the", "a", "uh", 嗯, 的, 了, ...), must match one run of the sentence's words in order, skipping only fillers and contraction remnants ("ve", "ll", "s", ...); a word that differs only by an inflection ("light"/"lights", "turn"/"turned") still matches.
- So an added negation, a different action, target or value, or a dropped word inside the run is not an echo: "Do not turn on the living room light", "Turn off the living room light", "turn on the kitchen light", "twenty degrees" against "twenty one degrees", 「不要打开客厅的灯」 and 「关闭客厅的灯」 against "I will turn on the living room light" / 「我会打开客厅的灯」 all reach the normal barge-in path.

Constants and rules: `weixue_patches.py`, Playback record and echo guard.

### Tests

`server/tests/conftest.py` puts `server/overlay` before the vendored `main/xiaozhi-server` on `sys.path` (override the clone location with `WEIXUE_VENDOR`), stubs `config.logger`, `core.handle.receiveAudioHandle` and `core.handle.reportHandle`, and runs every test from a scratch directory whose `core/` links to the overlay. Mocked by default; `RUN_LIVE_TESTS=1` adds the real-API smoke tests.

```bash
uv venv --python 3.10 .venv && uv pip install --python .venv/bin/python -r server/requirements-test.txt
. .venv/bin/activate && cd server
python -m pytest                                        # mocked, no network
(set -a; . ./.env; set +a; RUN_LIVE_TESTS=1 python -m pytest -s -k live)
```

The live tests take `ANTHROPIC_API_KEY` and `GOOGLE_SA_FILE` from `server/.env`, sourced in a subshell; never put keys on the command line. The ha-mcp smoke test (`-k mcp_bridge_live`) needs the running stack and `data/mcp-bridge/endpoint-url`.

`opuslib_next` needs `libopus` and `pydub` needs `ffmpeg`. Without a system copy, place `libopus.dylib` in `.venv/lib/` (the conftest adds it to `DYLD_FALLBACK_LIBRARY_PATH`) and `ffmpeg` in `.venv/bin/`.

## Google Cloud credentials

`GOOGLE_SA_FILE` in `server/.env` is the host path of the service-account JSON; compose mounts it read-only at `/run/secrets/google-sa.json`, the `credentials_file` of both Google providers. The key's project needs the Speech-to-Text and Text-to-Speech APIs enabled.

## Google Cloud STT streaming (`ASR.GoogleSTTStream`, `type: google_stt_stream`, selected)

`server/overlay/core/providers/asr/google_stt_stream.py`: Speech-to-Text v2 `StreamingRecognize`, model `latest_short`, location `us`, `language_codes: ["en-US", "cmn-Hans-CN"]`, interim results on, upstream `InterfaceType.STREAM`. It subclasses the batch provider below (credentials, client, recognizer, the 59 s limit and `too_long_reply` are shared).

- A stream opens on the first frame the VAD hears as voice and receives the buffered pre-roll (upstream keeps the last 10 frames, 0.6 s, while idle), then every frame as it arrives. The stream is half-closed when the VAD declares the end of the utterance (`client_voice_stop`; in `manual` listen mode on `listen stop`), and the transcript is the final results of the stream. Utterances of 15 frames or fewer are dropped, as upstream does.
- `latest_short` ends a stream at its own end of utterance (`END_OF_SINGLE_UTTERANCE`). When that happens while the VAD still hears the utterance, the next voiced frame opens a follow-on stream that first resends the last 2 frames received before the end was noticed; the finals of all streams are joined.
- An audio-state reset during the utterance (`reset_audio_states`) cancels the open stream. Past 59 s of buffered audio the stream is cancelled and the voice stop speaks `too_long_reply`.
- A follow-on stream starts 2 frames before the frame holding the finished stream's reported end of utterance (`speech_event_offset`), or, without one, 2 frames before the frame on which the end was noticed.
- After the half-close the provider waits up to `final_timeout` for the stream to end, then re-recognizes the buffered utterance with `fallback_model` batch `Recognize` (within `fallback_timeout`) when:
  - a stream errored, could not open, or timed out (`stream_error`, `no_stream`, `timeout`);
  - the last stream's end of utterance lies more than 0.6 s (`TRUNCATED_TAIL_S`) before the end of the audio it was given (`truncated`: `latest_short` ends its single utterance at the pause after "Jarvis," and drops the request);
  - a wake segment with live voice (`stt_segment kind=wake`) was heard as only a `wakeup_words` entry (`wake_word_only`, punctuation and case ignored);
  - the streams ended with no non-empty final on an utterance over 15 frames (`no_final`: a final without alternatives, or no response at all);
  - a final is detected as Chinese (`cmn`/`zh`/`yue` language code, or Han characters) with confidence below `zh_fallback_confidence`.

  An utterance that barge-in off drops (it started during a reply, see Barge-in) is not re-recognized (`chirp_3 fallback (<problem>) skipped`).
- After `stream_error`, `no_stream`, `timeout`, `truncated` or `wake_word_only` the streamed finals are never used: only the complete-buffer transcript is, and when that re-recognition fails too (or `fallback_model: ""`) the utterance is rejected: no turn, and the device speaks `repeat_reply` (default "Sorry, I didn't catch that. Please say it again." / 「抱歉，我没听清，请再说一遍。」). After `no_final` an empty or failed re-recognition is no turn and no repeat prompt. A Chinese low-confidence re-recognition that fails or returns nothing keeps the streamed text. `fallback_model: ""` disables the fallback.
- Every recognized utterance logs one `stt_stream <i> burst=<frames queued at open> frames=<n> bytes=<n> sent=<requests> responses=<n> interims=<n> finals=<n> empty_finals=<n> utterance_end_ms=<audio offset> first_interim_ms=<ms> half_close_ms=<ms> end_ms=<ms> service_end=<0|1> cancelled=<0|1>` line per stream (times from the stream's open; `service_end`: the service ended it before the half-close), then `stt_final_latency_ms=<ms> model=<model> language=<code> confidence=<lowest final> fallback=<-|zh_low_confidence|stream_error|no_stream|timeout|truncated|wake_word_only|no_final> streams=<n> interims=<n>`, with ` no_final=<no_response|no_speech|empty_final>[_service_end]` after `no_final`, and `model=-` and ` rejected=fallback_failed` for a rejected one; the latency runs from the VAD's end of utterance to the final text, fallback included. Cancelled streams log `Google STT streams dropped: <audio_reset|over_limit|close> streams=<n>`.

| Key | Value | Notes |
|---|---|---|
| `model` | `latest_short` | |
| `interim_results` | true | |
| `final_timeout` | 2 s | Wait for the stream end after the half-close |
| `fallback_model` | `chirp_3` | Empty disables the fallback |
| `fallback_timeout` | 15 s | Batch `Recognize` deadline |
| `zh_fallback_confidence` | 0.9 | `latest_short` reported 0.91 on every Mandarin final, right or wrong (`docs/logs/feat-stt-streaming-accuracy-2026-10-07.md`), so 0.9 never re-recognizes and any value above 0.91 re-recognizes every Chinese utterance. The English-labelled-as-Mandarin finals of the 2026-10-11 session were wake-only or truncated segments, which the wake-only drop and `truncated` now handle |
| `repeat_reply` | English then Mandarin "didn't catch that" line | Spoken when a stream error, timeout, truncation or `wake_word_only` is not followed by a successful re-recognition |

Switch back to batch recognition with `VOICE_ASR=GoogleSTT` in `server/.env`. The accuracy check `server/scripts/stt_accuracy.py` (usage in its docstring) synthesizes 10 English and 10 Mandarin sentences and compares `latest_short` streaming with `chirp_3` batch. The live tests (`-k live`) stream the two clips in `tests/assets/` in real time.

### ASR segment probe and capture

Every segment that reaches ASR (both providers) logs `stt_segment kind=<wake|wake_only|follow> frames=<n> seconds=<s> burst=<n> peak_dbfs=<db> rms_dbfs=<db>`: `kind` is `wake` / `wake_only` for the first segment after a wake detect with / without live voice (see Wake cue), else `follow`; `burst` counts the leading frames handed to ASR within 20 ms of each other (the device's wake-word burst). With `WEIXUE_STT_CAPTURE=1` in `server/.env` (passed to the container by `docker-compose.yml`) each segment is also written to `server/data/captures/` (gitignored) as `<UTC time>-<seq>.wav` (16 kHz mono, the PCM handed to ASR) and a `.json` sidecar with those facts, the provider, the frame arrival offsets and, once recognized, `result` (text, model, fallback, no_final reason); the line then ends ` capture=<name>`. File names carry no device or session identity; the newest 500 pairs are kept (`WEIXUE_STT_CAPTURE_MAX`).

`server/scripts/stt_replay.py` (usage in its docstring) replays captures through `latest_short` streaming (`--stream-model` for another model; pacing `recorded` from the sidecar, `realtime` or `burst`) and `chirp_3` batch, applies the provider's `no_final` and `truncated` rules (`stream+fallback`) and, for sidecars with `expected`, prints word errors per provider. `--synthesize DIR` writes a TTS replay set of the 2026-10-11 session phrases at several levels. `server/scripts/proactive_sim_device.py --wake Jarvis --wake-audio <wav>` sends the first 2.34 s of the WAV at once before the `listen detect`, as the firmware sends the wake-word audio and the packets held after it, and streams the rest in real time.

## Google Cloud STT batch (`ASR.GoogleSTT`, `type: google_stt`)

`server/overlay/core/providers/asr/google_stt.py`: Speech-to-Text v2, model `chirp_3`, location `us`, synchronous `Recognize` on the 16 kHz mono PCM the server already produces, `language_codes: ["en-US", "cmn-Hans-CN"]`. Only an `InvalidArgument` that rejects the language list (a `language_codes` field violation, "Bad language code", or "The language … is not supported by the model") is retried once with `["auto"]`, within what is left of the same `timeout`; any other error ends the turn with no transcript. An utterance over 59 s (`Recognize` accepts at most 60 s) is not sent or trimmed: the receive buffer stops growing one frame past the limit, the provider logs a warning, skips the LLM turn and the device speaks `too_long_reply` (default, English then Mandarin: "Sorry, that was too long for me. Please say it again, a bit shorter." / "抱歉，这段话太长了，请说短一点再试一次。"). The live test sends the two 16 kHz clips in `tests/assets/` and expects "turn on the living room lights" in English and Mandarin.

## Google Cloud TTS (`TTS.GoogleTTS`, `type: google_tts`)

`server/overlay/core/providers/tts/google_tts.py`: Chirp 3 HD, one speaker for both locales (`speaker`, default `Charon`): CJK text goes to `cmn-CN-Chirp3-HD-<speaker>`, Latin text to `en-US-Chirp3-HD-<speaker>`, anything else to `default_language` (`en` | `zh`). The streaming path uses `streaming_synthesize` with raw PCM at the connection sample rate, one request per text segment, 60 ms Opus frames. The first segment of a reply follows `first_clause` (default `true`): it ends at the earliest clause or sentence mark (`， , 、 ： : ； ; 。 . ! ? ！ ？ … —`) whose prefix holds at least 2 English words or 4 Chinese characters (a `.` counts when whitespace, a closing quote or bracket, or the end of the text follows it and it ends no abbreviation or single-letter initial; `.`, `,` and `:` after a digit wait for the next character); without one it ends at the last word boundary once the text holds 8 words, or at once with 12 Chinese characters. So "On it." and "Let me check." go to synthesis as soon as they arrive. `first_clause: false` restores the rule below for the first segment too. For later segments, besides upstream's punctuation, a segment ends at an English sentence end (`.`/`…` followed by whitespace; abbreviations such as "Dr." and single-letter initials excluded, decimals never match), and unpunctuated text is cut at a word boundary once it exceeds 250 characters. A request is split to at most 5,000 UTF-8 bytes, preferring a sentence end, then a clause mark, then whitespace, then a character boundary. Each streaming call is cancelled within 50 ms of an abort, a newer turn (`sentence_id`) or `close()`, also while the first response is pending; a cancellation the provider did not request is logged as an error and still ends the turn; the sync path (`to_tts`, wake-word cache) uses `LINEAR16`. `speaking_rate` (0.25-2.0, or the `ttsRate` percentage) is the only streaming-path knob. A relative `credentials_file` resolves against the server directory; without one the client uses `GOOGLE_APPLICATION_CREDENTIALS`.

## Voice LLM switch (`VOICE_LLM`)

`VOICE_LLM` in `server/.env` selects the voice LLM path when `scripts/render_config.py` renders the config (default `anthropic`; `setup.sh` applies a change):

| `VOICE_LLM` | `selected_module.LLM` | `selected_module.Intent` | Voice path |
|---|---|---|---|
| `anthropic` | `AnthropicLLM` | `function_call` | xiaozhi's own tool loop: Anthropic Messages API, mcp-bridge voice tools, device MCP tools |
| `claude_agent` | `ClaudeAgentLLM` | `nointent` | every turn goes to agent-service (`docs/agent-service.md`); HA calls and device actions run there |

`validate_config.py` refuses a `claude_agent` LLM under any Intent but `nointent`. Under `claude_agent` the following stay in the tree, selectable for rollback with `VOICE_LLM=anthropic`, and have no effect: `decode_direct_answer`, `keep_decoded_text`, `_hex4`, `_RESPONSE_VALUE` and `_JSON_ESCAPES` in `weixue_patches.py` (`direct_answer` exists only under `function_call`); `overlay/core/providers/llm/anthropic/anthropic.py`; the bridge's voice tool path (`mcp-bridge/weixue_bridge/voice_filter.py`, `policy/voice_bridge.json`); the MCP endpoint executor's timeouts on the voice path. The bridge keeps running for its health check and the monitor. Every other patch in `weixue_patches.py` applies to both paths.

## LLM: agent-service gateway (`LLM.ClaudeAgentLLM`, `type: claude_agent`)

`server/overlay/core/providers/llm/claude_agent/claude_agent.py`. Upstream `chat()` calls `response()` and treats every yield as reply text. The provider sends only the last user message: agent-service keeps the conversation, and xiaozhi's local `dialogue` is not used. `response()` runs in `chat()`'s executor thread and reads a queue that an `AgentTurn` coroutine on the connection's event loop fills (aiohttp). URL and bearer are `AGENT_SERVICE_URL` and `AGENT_SERVICE_TOKEN` from the container environment; neither is in `.config.yaml`.

- **Request**: `POST {AGENT_SERVICE_URL}/v1/chats/current/messages`, `Accept: text/event-stream`, `Idempotency-Key: <device>:<session_id>:<turn seq>`, a new 32-hex `X-Trace-Id`; body `{device_id, text, lang, origin: {kind: "voice", gateway_session_id, turn_seq}}`; `lang` from the STT language of the utterance, else `zh` for Chinese text, `en` for Latin text, else `auto`. Connect timeout 2 s, read timeout 6 s (`STALL_S`, three missed `: hb` frames).
- **Chat**: always `current`. When the device's `switch_chat` or `new_chat` post to agent-service is still in flight (`ServiceSink.pending_switch`), the request waits for it up to 1 s (`SWITCH_WAIT_S`).
- **Stream**: each `text` delta of the turn (`turn_id` of `accepted`) is yielded as it arrives; each heartbeat yields `""`, so `chat()` re-checks `client_abort` at least every 2 s. The turn ends at the `reply_end` of its `reply_id`, whatever its reason (`turn_done`, `hold_elapsed`, `ack`, `question`, `gateway_closed`); then `chat()` sends LAST and the normal 8 s no-voice window runs. The provider speaks nothing of its own while the stream is alive.
- **Barge-in**: an abort (`handleAbortMessage`: wake, tap, IMU, or with `barge_in: true` a transcript while the server holds the turn), a newer `chat()` on the connection and the end of the session close the open stream at once from the event loop; text not yet yielded is dropped, and a stream of a superseded `sentence_id` yields nothing more. agent-service handles the closed stream as `gateway_closed` (`docs/agent-service.md`, Steer, cancel, barge-in); the next utterance is the next request. The provider never calls `/interrupt`.
- **device_action**: a `device_action` event `{action_id, tool, args}` on the turn's stream runs the device MCP tool on the connection (`call_mcp_tool`, 6 s) when `tool` is agent-visible (`weixue_device_tools.AGENT_VISIBLE_DEVICE_TOOLS`, DESIGN.md ADR-13), and posts `{ok, result | error}` to `/v1/chats/{chat}/device_actions/{action_id}`. An `action_id` the turn already started (an event replayed by a resume) is not run again. Any other tool is refused without reaching the device. Logged as `device_action <tool> ok=<bool> action=<id> trace=<trace>`.
- `response_no_stream` (memory and intent providers) returns `""`: the provider serves voice turns only.

Errors (one log line each, never the token):

| Case | Action |
|---|---|
| Connect error or 5xx (not 503 with `detail.speak`) before the stream opens, or a stream that ends before `accepted` | one retry after 300 ms with the same Idempotency-Key, then the fallback line |
| 503 with `detail.speak` (busy) | that line; no retry; not a breaker failure |
| 400, 401, 404, 409, 429 | the fallback line |
| Read stall (6 s) or drop after `accepted` | once: `GET /v1/chats/{chat}/events?after=<last seq>`, this turn's text until its `reply_end`; a resumed `reply_end` of `gateway_closed`, or a second stall or drop, speaks the fallback line |
| 3 failures within 60 s | breaker: for 30 s no request, the fallback line at once; `breaker open` / `breaker closed` log lines |

The fallback line is "I can't reach my assistant right now." / 「我暂时连不上助手。」, or "Sorry, I lost the connection." / 「抱歉，连接断了。」 once reply text was spoken. It goes into the turn's sentence (`sentence_id`) directly and never into the dialogue history.

Per turn, once its audio finished (or the turn was superseded, aborted or 120 s passed), one line:

```
turn_latency trace=<hex> chat=<id> routed=<routed> reply_end=<reason|fallback|busy|breaker|closed> stt_final_latency_ms=<ms> service_first_text_ms=<ms> tts_first_audio_ms=<ms> total_ms=<ms> answer_audio_ms=<ms> reply_end_ms=<ms> barge_in=<0|1>
```

| Key | From → to |
|---|---|
| `stt_final_latency_ms` | VAD end of utterance → final transcript (`GoogleSTTStream` value of the utterance) |
| `service_first_text_ms` | request sent → first non-empty `text` |
| `tts_first_audio_ms` | first segment handed to TTS → its first audio packet written to the device |
| `total_ms` | VAD end of utterance → first audio packet written |
| `answer_audio_ms` | VAD end of utterance → first packet of the first segment after the turn's last Home Assistant or server tool (`progress` phase `tool`); equals `total_ms` without one |
| `reply_end_ms` | request sent → `reply_end` |
| `barge_in` | the connection's `barge_in` (Barge-in) |

`-` marks a value the turn does not have (no utterance timing for a text turn, no audio after an abort).

Sim rounds: `server/sim/agent_scenarios.sh live` sends "What time is it?" from the scripted ASR through the deployed agent-service (the sim server joins the live compose network; nothing there restarts); `m2` runs, against the deployed service and each in a new chat, a voiceless task of 2–4 minutes (50 sequential Home Assistant reads) that finishes while the sim device is offline and is spoken WHEN_IDLE through the gateway once the device polls again, a running task steered ("How is it going?") and then stopped, a barge-in while the answer plays (the transcript aborts the turn and closes its stream), and one turn with the sim server re-rendered for `VOICE_LLM=anthropic`; `branch` builds this tree's agent-service beside the sim server and adds "Set bedside mode." (`device_action` → the sim device's MCP `tools/call`, `proactive_sim_device.py --mcp-tools`) and a stopped service (fallback line). Compose project `weixue-agent-sim`, `127.0.0.1:18200/18203`, config `server/sim/agent.config.yaml` rendered by `render_config.py` (`VOICE_LLM`, default `claude_agent`; `VOICE_BARGE_IN`, default `1` for the barge round).

Barge-in rounds (no credentials): `server/sim/barge_scenarios.sh [TRANSCRIPT]` renders `server/sim/barge.config.yaml` (scripted ASR, `SimLLM` three-sentence reply, tone TTS) for `VOICE_BARGE_IN=0` and then `1` in compose project `weixue-barge-sim` (`docker-compose.barge.yml`, `127.0.0.1:18300/18303`) and runs: an utterance 1 s into the reply's audio (0: dropped, no abort, the reply plays to its end and an utterance after it is answered; 1: the reply is cut and the utterance answered), and an utterance that starts at the wake cue's first audio frame (answered with either value).

Gateway recovery rounds (no Docker): `server/sim/scratch_agent_service.py` runs agent-service on a temporary database with the scripted model and fake ha-mcp of `server/agent-service/tests`, a 3 s offer lease and fault injection (`POST /sim/faults`: the next n requests to a path answer a status, optionally after being handled), started with the agent-service test venv; `server/sim/gateway_recovery_sim.py` (server test venv) runs the real gateway and proactive queue against it over HTTP with simulated device sessions: card answers under 503 and a lost reply, speech hand-off with failed `accepted` posts, a delivery that fails before audio and is offered again, a gateway restart restoring the device snapshot, INTERRUPT during a voice turn and over a WHEN_IDLE announcement, and open cards reconciled against the service's task state at a device reboot (a question card whose task was cancelled while the delivery stream was down, a card of a task the service does not know, the service unreachable; `docs/logs/fix-mirror-reconcile-sim-2026-10-11.log`). Both take `SIM_TOKEN`; usage in their docstrings.

Held-call round (no Docker): `server/sim/held_call_sim.py` runs agent-service as `service.py` builds it against the fake ha-mcp over real TCP holding `wait_seconds=20`, and prints when the held call answers pending, when its card becomes approvable and the replay outcome (`docs/logs/fix-hamcp-client-timeout-sim-2026-10-11.log`).

## LLM: Anthropic Claude (`LLM.AnthropicLLM`, `type: anthropic`)

`server/overlay/core/providers/llm/anthropic/anthropic.py` streams with `client.messages.stream`, maps the server's OpenAI-style function list to `tools`/`input_schema`, and converts the dialogue history to `tool_use`/`tool_result` blocks (results are placed directly after their call; dangling calls get an `is_error` result; results with no matching call are dropped). Thinking blocks are never replayed.

| Key | Default | Notes |
|---|---|---|
| `api_key` | `ANTHROPIC_API_KEY` from `server/.env` | Required by `setup.sh`. |
| `model_name` | `claude-sonnet-5-5` | `claude-haiku-4-5-20251001` is the low-latency alternative. |
| `fallback_model_name` | unset | Tried once when the first request fails with 429/5xx/529 before any output. |
| `max_tokens` | 1024 | Thinking tokens count toward it. |
| `effort` | `low` | `low`-`max`; clamped to `high` under `thinking: between_tools`. Not sent to Haiku. |
| `thinking` | `adaptive` | `adaptive` (field omitted on the wire) or `between_tools` (Sonnet 5.5 only, no up-front reasoning). Not sent to Haiku. |
| `timeout` | 60 s | Number, or `{connect, read, write, pool}`. |
| `max_retries` | 1 | SDK retries (timeouts included). |

No `temperature`/`top_p` keys: Sonnet 5.5 rejects non-default sampling. Tool names must match `^[a-zA-Z0-9_-]{1,128}$`; others are skipped with a warning.

### System prompt (ADR-7)

`server/overlay/weixue-agent-prompt.txt` replaces upstream `agent-base-prompt.txt` via `prompt_template` in `config/config.template.yaml`; the household identity is the `prompt:` key there. Rules: reply in the language of the current turn, English when ambiguous, Simplified Chinese for Chinese; no narration before a tool call; device state only from Home Assistant tool results; one or two spoken sentences; no Markdown or emoji. The template keeps upstream's Jinja variables (`base_prompt`, `emoji_enabled`, `emojiList`, `today_date`, `dynamic_context`, ...), the literal `{{current_time}}` and the `<memory></memory>` block.

## Reproduce

Prerequisites: Rancher Desktop running (`~/.rd/bin` on PATH), python3, the two clones from `vendor.lock` under `~/Projects/weixue-vendor/`, ~4 GB free disk.

```bash
cd server
cp .env.example .env     # set LAN_IP, BIND_IP (Mac: 0.0.0.0), ALLOWED_DEVICES, TZ, TZ_OFFSET, ANTHROPIC_API_KEY, GOOGLE_SA_FILE, HA_MCP_URL (chmod 600)
./scripts/setup.sh
```

Required `.env` keys: `LAN_IP`, `BIND_IP`, `ALLOWED_DEVICES`, `TZ`, `TZ_OFFSET`, `ANTHROPIC_API_KEY`, `GOOGLE_SA_FILE`, `HA_MCP_URL` (`REQUIRED_KEYS` in `setup.sh`). After the pre-flight, `setup.sh` appends `AGENT_SERVICE_TOKEN` (32 random bytes, hex) when it is unset and never prints it, and keeps `AGENT_SERVICE_HOST=wg` in `.env` exactly while `WG_TUNNEL=1`.

`setup.sh` is idempotent. In order: a pre-flight that fails before any build, publish or container change when a required key is unset or empty, `GOOGLE_SA_FILE` is missing, or `BIND_IP` is not accepted (Network boundary); downloads SenseVoiceSmall (~936 MB) if missing, checks the vendor clone, starts `mcp-endpoint-server`, renders `config/config.template.yaml` with `scripts/render_config.py` (values are escaped; `@@NAME?@@` is optional and renders empty when unset) to a mode-0600 temporary file next to `data/.config.yaml`, tags the current `mcp-bridge` and server images (and `weixue/wg:local` while `WG_TUNNEL=1`) `:rollback`, builds them, and validates the rendered file with `scripts/validate_config.py` inside the new server image (YAML, `server.websocket`, auth allow-list, selected modules, LLM key, `mcp_endpoint`; problems are printed without values). A failed render, build or validation exits with `data/.config.yaml` and the `:local` images unchanged. Then it copies `data/.config.yaml` to `data/.config.yaml.rollback`, writes `data/mcp-bridge/endpoint-url` through a temporary file and a rename, renames the rendered file over `data/.config.yaml`, force-recreates the containers (`wg` too while `WG_TUNNEL=1`; otherwise it then removes the `wg` container) and checks deployment readiness:

1. `scripts/check_ready.py`, up to 90 s: OTA banner on 8003, WebSocket handshake and banner frame on 8000, at `BIND_IP`, or `127.0.0.1` for `0.0.0.0`.
2. While `WG_TUNNEL=1`, the `wg` healthcheck (handshake under 180 s old and the boundary chain hooked in), retried every 2 s for up to `DEPLOY_WAIT_S` (90 s).
3. The bridge's healthcheck (`python -m weixue_bridge.health`: it connected to the endpoint and registered ha-mcp's tools, and its last ping succeeded), same bound.
4. One read-only Home Assistant call along the voice path, made from the server container with the server's own endpoint URL: `scripts/check_ha_path.py` (`ha_get_state` for `zone.home` through the endpoint, the bridge, the tunnel when enabled, and ha-mcp; a state or `ENTITY_NOT_FOUND` counts as Home Assistant answering), same bound, 20 s per attempt. It prints one line and never a URL.
5. agent-service's healthcheck (`python -m weixue_agent.health`: database writable, last model request not failed, a successful ha-mcp ping within 60 s), same bound. A rollback of the first deployment that adds agent-service removes it and skips this check.

After all five pass it copies `.env` to `.env.deployed` (0600). If the recreate or any check fails, it restores the previous config, the endpoint unit (below) and images and, when `.env.deployed` exists, `.env` from it (the attempted file is kept as `.env.failed`, 0600), recreates the containers in that file's tunnel mode, runs the same checks against them and exits 1.

From the first change of `server/.env` on, the run is one transaction (`PHASE` in `setup.sh`, an `EXIT` trap; `.env` is copied to `.env.txn`, 0600, removed at exit). Any failure before publishing, an unexpected exit under `set -e` included, restores the `:local` images from `:rollback`, the endpoint key and URL file (recreating `mcp-endpoint-server` when its key changed, stopping it when this run started it), the agent-service database files it moved back to `data/agent-service/`, `docker start` for agent-service and agent-monitor when this run stopped them (database move or backup), and `server/.env` as it was at the start of the run, so tokens or profiles the failed run wrote are gone; it prints `setup failed (exit <n>, line <n>) before publishing; …` and exits non-zero. A failure after publishing started takes the rollback below (`setup failed … while publishing; rolling back`).

The endpoint key (`data/mcp-endpoint/.mcp-endpoint-server.cfg`), the URL derived from it and its two consumers (`mcp_endpoint` in `data/.config.yaml` and `data/mcp-bridge/endpoint-url`) are one deployment unit: `setup.sh` copies the key file and the URL file to `.rollback` (0600) before it starts `mcp-endpoint-server`; a rollback restores both with the config and, when the key changed during the attempt, recreates `mcp-endpoint-server` with the restored key. A successful run removes those two copies. If that second recreate also fails, the stack is down: it prints a `manual restore:` block (retag each `:rollback` image to `:local`, then `docker compose [--profile …] up -d --force-recreate …` from `server/`) and exits 1. `docker-compose.yml` is not rolled back; without `.env.deployed` (first run) neither is `.env`. Logs: `docker logs -f xiaozhi-esp32-server`.

Providers: VAD SileroVAD, ASR `GoogleSTTStream` (batch `GoogleSTT` stays configured), LLM `AnthropicLLM` or `ClaudeAgentLLM` (`VOICE_LLM`), TTS `GoogleTTS`. Upstream `FunASR` (SenseVoiceSmall, local), `EdgeTTS` and the OpenAI-compatible `StopgapLLM` (Groq, `LLM_*` in `.env`) stay configured and are selected by editing `selected_module`. `wakeup_words` adds `Jarvis` to the upstream list.

Optional web console (MySQL + Redis + manager-web, port 8002): set `MYSQL_ROOT_PASSWORD` in `.env`, then `docker compose --profile console up -d`. Not validated.

## Agent service

Compose service `agent-service` (`docs/agent-service.md`): no host port; the server container reaches it at `AGENT_SERVICE_URL` (`http://agent-service:8010`, or `http://wg:8010` while `WG_TUNNEL=1`, where it shares the tunnel namespace like `mcp-bridge`) with the bearer `AGENT_SERVICE_TOKEN`, and the agent gateway (`docs/agent-protocol.md`) consumes its outbox for every device with an agent session. Its environment holds `ANTHROPIC_API_KEY`, `HA_MCP_URL`, `AGENT_SERVICE_TOKEN`, `MONITOR_TOKEN`, `AGENT_DB`, the ha-mcp probe cadence, the ha-mcp tool-call read timeout `HA_MCP_READ_TIMEOUT_S` (default 35 s; it must outlast the ha-mcp policy `wait_seconds`, see `docs/agent-service.md`) and `TZ`; data lives in `data/agent-service/` (database in `db/`).

## Agent monitor

Compose service `agent-monitor` (`docs/agent-monitor.md`): a read-only board at `http://127.0.0.1:8020/`, bound to 127.0.0.1 only (decision 30); from a phone through `ssh -L 8020:127.0.0.1:8020`, on the Ubuntu server later through Tailscale. Tiles turn red on the rules in `docs/agent-monitor.md` (device 3 s after its last poll, agent after 2 failed checks, ha-mcp after 25 s without a good ping, bridge after 65 s or on `stopped`). It stores nothing; the agent service prunes event tables after 30 days (decision 21).

## Verification

- `curl http://<LAN_IP>:8003/xiaozhi/ota/` returns the OTA banner; `POST` returns the OTA response (Firmware endpoints).
- Port 8000 accepts WebSocket; a `hello` returns `session_id`; a `listen/detect` text turn returns `stt`, `tts start/sentence_start/stop` and Opus frames.
- Not verified without hardware: ASR on live microphone audio.

## Office / off-LAN mode (WireGuard tunnel)

When the Mac is off the home LAN, `mcp-bridge` reaches ha-mcp through a WireGuard client sidecar, compose service `wg` (profile `tunnel`, image `weixue/wg:local` built from `server/wg/`: pinned Alpine + `wireguard-tools`, kernel WireGuard). The bridge and agent-service share the sidecar's network namespace (`network_mode: service:wg`); the server, `mcp-endpoint-server` and the device path are unchanged. The bridge still reaches `ws://mcp-endpoint-server:8004` by name: the sidecar sits on the project network and keeps Docker's resolver.

Prerequisites:

- The router's WireGuard server (GL.iNet) is reachable from the WAN on its UDP port.
- `server/.secrets/wg0.conf` (mode 0600, directory 0700) is the router's client config with `AllowedIPs = 192.168.8.0/24` only, so nothing but home-LAN traffic enters the tunnel. MTU 1420 (the `MTU` line, or wg-quick's default on a 1500-byte path).
- `DNS` lines in the config are ignored; `HA_MCP_URL` uses an IP address.

| Item | Behaviour |
|---|---|
| Config | Mounted read-only at `/etc/wireguard/wg0.conf` (`create_host_path: false`: a missing file fails the start instead of creating a directory). The sidecar brings `wg0` up from a copy without `DNS` lines on a tmpfs |
| Start | A failed `wg-quick up` (for example the endpoint name not resolving at boot) is retried every 10 s with one redacted log line per failure; the container keeps running |
| Keepalive | Peers without `PersistentKeepalive` get 25 s, so the sidecar handshakes without bridge traffic |
| Boundary | Before `wg-quick up`, the sidecar adds chain `WG_BOUNDARY` to `OUTPUT` (iptables, ip6tables for IPv6 entries) that drops every packet to an `AllowedIPs` network leaving through any interface other than `wg0`. Home-LAN traffic from the namespace (the bridge's HA requests) therefore never takes the default route: not while `wg-quick up` is failing, not after `wg0` is removed, and not in a replacement namespace before its tunnel is up. A config whose `AllowedIPs` holds a default route (`0.0.0.0/0`, `::/0`) or no `AllowedIPs` stops the sidecar. Evidence with scratch containers: `docs/logs/fix-wave5-server-c7-tunnel-boundary-2026-10-07.log` |
| Healthcheck | Healthy while the boundary chain is hooked into `OUTPUT` and the latest handshake is under 180 s old; 60 s start period |
| Logs | `docker logs wg` prints one line (`wg0 up, N peer(s), mtu M`); wg-quick errors are printed with addresses, keys, endpoints and quoted values redacted |
| Bridge | Compose starts it once `wg` is healthy (`depends_on` with `required: false`, so it does not apply without the profile). In tunnel mode (`TUNNEL_IFACE=wg0`) it connects to nothing until `wg0` exists in the namespace. In both modes it exits non-zero after `ENDPOINT_MAX_FAILURES` consecutive failed endpoint connects (`mcp-bridge/weixue_bridge/config.py`), and when its `eth0` (tunnel mode: or `wg0`) is gone after an endpoint connection ends; `restart: unless-stopped` starts it again, in tunnel mode inside the sidecar's current namespace |

Enable: set `WG_TUNNEL=1` in `server/.env`, then `./scripts/setup.sh`. It checks that `.secrets/wg0.conf` exists with mode 0600 (it never reads the file), adds `tunnel` to `COMPOSE_PROFILES` in `server/.env` (other profiles are kept) and recreates `wg`, `mcp-bridge` and the server. `server/.env` then holds both lines:

```bash
WG_TUNNEL=1
COMPOSE_PROFILES=tunnel
```

so every plain `docker compose` command in `server/` includes `wg`. A `COMPOSE_PROFILES` exported in the shell overrides `.env` and must then contain `tunnel`; without it compose refuses with `depends on undefined service "wg"`. Compose treats any non-empty `WG_TUNNEL` as on; `setup.sh` rejects every value other than empty and `1`. Recreating only the tunnel path:

```bash
docker compose up -d --build wg mcp-bridge
docker ps --filter name=wg --filter name=mcp-bridge    # both (healthy)
```

Disable: set `WG_TUNNEL=` and run `./scripts/setup.sh`, which removes `tunnel` from `COMPOSE_PROFILES`, moves the bridge back to the project network and removes the `wg` container; by hand, delete both lines, then `docker compose up -d mcp-bridge && docker rm -f wg`.

Restarts: compose recreating or restarting `wg` restarts `mcp-bridge` with it. After any other `wg` restart (`docker restart wg`, a crash) the bridge loses `eth0`, exits once its endpoint connection drops and rejoins the new namespace on its own; `docker restart mcp-bridge` only skips that wait.

Not covered: inbound traffic from the home LAN to the Mac, such as the proactive webhook (`docs/proactive.md`), still needs the LAN path.

## Migrate to Ubuntu

1. Install Docker Engine + compose plugin; clone this repo; clone both repos from `vendor.lock` at the pinned commits into `~/Projects/weixue-vendor/`.
2. Copy `server/.env` (secrets, 0600) and `server/data/` (`.config.yaml`, `mcp-endpoint/` key, `models/`). Copying `mcp-endpoint/` keeps MCP URLs valid; omit `models/` to let `setup.sh` re-download.
3. Copy the Google service-account JSON (0600) and point `GOOGLE_SA_FILE` at it.
4. Set `LAN_IP` and `BIND_IP` in `.env` to the Ubuntu host's address (the ports then bind to that address only) and run `./scripts/setup.sh`.
5. Re-point the firmware `OTA_URL` to the new address, or give the host the old IP / a DNS name. MCP clients on other hosts need an SSH tunnel or a changed 8004 binding.
6. Open TCP 8000 and 8003 on the host firewall.

## Timezone

The server is configured for `Europe/Dublin` (`TZ` in `server/.env`, required by `setup.sh`). The device follows daylight saving time from `server_time.tz` (see the OTA response above). `TZ_OFFSET` (whole hours) only sets `server_time.timezone_offset`, which the firmware applies when `tz` is absent; with `tz` present it needs no change at DST transitions.

## ASR selection

`VOICE_ASR` in `server/.env` selects the recognizer: `GoogleSTT` (chirp_3 batch, default) or `GoogleSTTStream` (latest_short streaming). Streaming is off by default since 2026-10-11 until the dropout fix lands (`docs/logs/session-2026-10-11-0655-rca.md`).
