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

`GET` on the OTA URL returns a plain-text banner. The device `POST`s JSON with `Device-Id` and `Client-Id` headers and receives the *OTA response*: `{"server_time":…,"firmware":…,"websocket":{"url":"ws://<LAN_IP>:8000/xiaozhi/v1/","token":""}}`.

Auth is disabled (`server.auth.enabled: false`). feat-firmware-base enables `server.auth.enabled` and `allowed_devices` once the device MAC is known.

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

`server/overlay/` is the only way to add or change server code. Its contents mirror `main/xiaozhi-server/` of the upstream clone (for example `server/overlay/core/providers/llm/<name>/<name>.py`) and are copied over the upstream tree at image build (`server/Dockerfile`, `__pycache__` excluded from both). Python packages the overlay needs beyond the upstream base image are pinned in `server/requirements-overlay.txt`, installed by the same Dockerfile. Apply with `./scripts/setup.sh`. The vendored clone is never edited.

Changes to upstream functions that are too small to justify overriding a whole module go in `server/overlay/weixue_patches.py`. The image's entrypoint, `weixue_main.py`, applies them and then runs upstream `app.py`. Current patches:

- `ConnectionHandler._clean_response_garbage` keeps a streamed chunk's trailing whitespace when the chunk ends in non-CJK text and no JSON closing characters were removed from its end. Upstream strips it from every chunk, so English words at chunk edges run together ("isfour").
- `ConnectionHandler.handle_connection`, `ListenTextMessageHandler.handle` and the aiohttp app of `core.http_server` are wrapped for proactive messages (`docs/proactive.md`).
- `ListenTextMessageHandler.handle` is also wrapped for the wake cue, and `core.providers.vad.silero.VADProvider.is_vad` for the wake utterance hold (see Wake cue).
- `ConnectionHandler.just_woken_up` is a property that always reads `False`: upstream sets it on every `listen detect` and then drops the next 2 s of microphone audio.
- `receiveAudioHandle.no_voice_close_connect`: an audio frame received while the server holds the turn (`client_is_speaking`, from the STT result or wake cue to `tts stop`) refreshes `last_activity_time`, so the idle close counts from the end of the reply (see Conversation timing).
- `McpTextMessageHandler.handle` logs every device MCP notification whose method starts with `notifications/weixue/` as `device event <method> device=<mac> params=<json>` before upstream handles it (panel decisions, `docs/firmware.md` Monitor panel; IMU gestures).
- `intentHandler.checkWakeupWords` is replaced: a transcript that is only a `wakeup_words` entry (case-insensitive, punctuation ignored), or only a cue word (`CUE_ECHO_WORDS` plus the `wake_cues` texts) within 5 s of a cue starting, is dropped with no reply. `enable_wakeup_words_response_cache` and the cached `wakeup_words` clips are therefore unused.

## Conversation timing (ADR-7)

| Key | Value | Effect |
|---|---|---|
| `close_connection_no_voice_time` | 8 | Seconds without user voice after the reply or wake cue ends (or after the wake event) before the server closes the WebSocket; the device returns to idle on close. The connection-level timeout is this value + 60 s |
| `end_prompt.enable` | false | The idle close sends no farewell turn |
| `wake_cues` | 3 clips | See Wake cue; with an empty list a wake is handled by upstream |
| `enable_greeting` | false | Without `wake_cues`, a wake gets `stt` + `tts start/stop` and no audio |
| VAD `SileroVAD.min_silence_duration_ms` | 200 (upstream) | End-of-utterance silence; not overridden |

The device listens in realtime mode and streams microphone audio for the whole session, so the server's VAD sees the follow-up window; a follow-up inside it needs no wake word. Wake-word detection is off while the device listens (`CONFIG_WAKE_WORD_DETECTION_IN_LISTENING` unset).

### Wake cue

`wake_cues` lists `{file, text}` clips: `server/overlay/config/assets/wake_cues/{hmm,mm-hm,yes}.wav` ("Hmm?", "Mm-hm?", "Yes?"), rendered once with the live Google TTS voice (en-US Chirp 3 HD, `speaker` of `TTS.GoogleTTS`), trimmed to 0.46-0.60 s, 24 kHz mono. On a `listen detect` whose text is a wake word:

- One clip is picked at random. 0.6 s after the detect it plays as `tts start`, then (0.2 s later, after the device's decoder reset on entering Speaking) `sentence_start` with the clip text plus its audio, then `tts stop`. The caption shows the clip text.
- The cue is skipped when the VAD heard voice in live audio from 0.25 s after the wake's `listen start` (the user went on speaking: "Jarvis, what is the weather like in London today?").
- Neither the detect nor the following `listen start` resets the audio state, so the wake-word audio the device sends before the detect (`CONFIG_SEND_WAKE_WORD_DATA`, 1.98 s ending at detection) reaches VAD and ASR together with the live audio after it. For 0.8 s after that `listen start` the VAD cannot end the utterance. A transcript that is only the wake word is dropped.
- The microphone is never muted; speech during the cue reaches ASR.

The device captures no audio between wake detection and listening start (286-330 ms measured), and live audio in the first ~0.6 s after it transcribes poorly, so a short request said in one breath with the wake word ("Jarvis, what time is it?") usually reaches the LLM clipped or not at all; longer ones lose their first words (`docs/logs/conversation-timing-onebreath-2026-10-07.log`).

Re-render the clips with `server/scripts/render_wake_cues.py` (usage in its docstring; runs in the server container).

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

## Google Cloud STT (`ASR.GoogleSTT`, `type: google_stt`)

`server/overlay/core/providers/asr/google_stt.py`: Speech-to-Text v2, model `chirp_3`, location `us`, synchronous `Recognize` on the 16 kHz mono PCM the server already produces, `language_codes: ["en-US", "cmn-Hans-CN"]` (one retry with `["auto"]` if the list is rejected). An utterance over 59 s (`Recognize` accepts at most 60 s) is not sent or trimmed: the provider logs a warning, skips the LLM turn and the device speaks `too_long_reply` (default: "Sorry, that was too long for me. Please say it again, a bit shorter."). The live test sends the two 16 kHz clips in `tests/assets/` and expects "turn on the living room lights" in English and Mandarin.

## Google Cloud TTS (`TTS.GoogleTTS`, `type: google_tts`)

`server/overlay/core/providers/tts/google_tts.py`: Chirp 3 HD, one speaker for both locales (`speaker`, default `Charon`): CJK text goes to `cmn-CN-Chirp3-HD-<speaker>`, Latin text to `en-US-Chirp3-HD-<speaker>`, anything else to `default_language` (`en` | `zh`). The streaming path uses `streaming_synthesize` with raw PCM at the connection sample rate, one request per text segment, 60 ms Opus frames; the sync path (`to_tts`, wake-word cache) uses `LINEAR16`. `speaking_rate` (0.25-2.0, or the `ttsRate` percentage) is the only streaming-path knob. A relative `credentials_file` resolves against the server directory; without one the client uses `GOOGLE_APPLICATION_CREDENTIALS`.

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
cp .env.example .env     # set LAN_IP, TZ/TZ_OFFSET, ANTHROPIC_API_KEY, GOOGLE_SA_FILE, HA_MCP_URL (chmod 600)
./scripts/setup.sh
```

`setup.sh` is idempotent. In order: downloads SenseVoiceSmall (~936 MB) if missing, checks the vendor clone, starts `mcp-endpoint-server`, writes `data/mcp-bridge/endpoint-url`, renders `config/config.template.yaml` to `data/.config.yaml` with `scripts/render_config.py` (values are escaped; `@@NAME?@@` is optional and renders empty when unset), then builds and force-recreates the `mcp-bridge` and server containers. Logs: `docker logs -f xiaozhi-esp32-server`.

Providers: VAD SileroVAD, ASR `GoogleSTT`, LLM `AnthropicLLM`, TTS `GoogleTTS`. Upstream `FunASR` (SenseVoiceSmall, local), `EdgeTTS` and the OpenAI-compatible `StopgapLLM` (Groq, `LLM_*` in `.env`) stay configured and are selected by editing `selected_module`. `wakeup_words` adds `Jarvis` to the upstream list.

Optional web console (MySQL + Redis + manager-web, port 8002): set `MYSQL_ROOT_PASSWORD` in `.env`, then `docker compose --profile console up -d`. Not validated.

## Verification

- `curl http://<LAN_IP>:8003/xiaozhi/ota/` returns the OTA banner; `POST` returns the OTA response (Firmware endpoints).
- Port 8000 accepts WebSocket; a `hello` returns `session_id`; a `listen/detect` text turn returns `stt`, `tts start/sentence_start/stop` and Opus frames.
- Not verified without hardware: ASR on live microphone audio.

## Migrate to Ubuntu

1. Install Docker Engine + compose plugin; clone this repo; clone both repos from `vendor.lock` at the pinned commits into `~/Projects/weixue-vendor/`.
2. Copy `server/.env` (secrets, 0600) and `server/data/` (`.config.yaml`, `mcp-endpoint/` key, `models/`). Copying `mcp-endpoint/` keeps MCP URLs valid; omit `models/` to let `setup.sh` re-download.
3. Copy the Google service-account JSON (0600) and point `GOOGLE_SA_FILE` at it.
4. Set `LAN_IP` in `.env` to the Ubuntu host's address and run `./scripts/setup.sh`.
5. Re-point the firmware `OTA_URL` to the new address, or give the host the old IP / a DNS name. MCP clients on other hosts need an SSH tunnel or a changed 8004 binding.
6. Open TCP 8000 and 8003 on the host firewall.

## Timezone

The server is configured for `Europe/Dublin` (`TZ`/`TZ_OFFSET` in `server/.env`). `TZ_OFFSET` is a fixed integer that the device receives once at boot via OTA; it must be changed to `0` when DST ends (2026-10-25) and back to `1` on 2027-03-28 until `fix-wave3-firmware` lands time resync with a POSIX TZ string.
