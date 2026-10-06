# Backend server (feat-server)

xiaozhi-esp32-server 0.9.7 in Docker Compose on the Mac (Rancher Desktop). The server image is built from the upstream clone at `${WEIXUE_VENDOR:-~/Projects/weixue-vendor}/xiaozhi-esp32-server`, pinned in `vendor.lock`; `setup.sh` refuses to build if the clone is not at that commit or has local changes. The resulting image carries the commit in its `org.opencontainers.image.revision` label. Other images are pinned by digest in `vendor.lock`, `docker-compose.yml` and `.env.example`.

## Firmware endpoints

Replace `<LAN_IP>` with `LAN_IP` from `server/.env`.

| Purpose | URL |
|---|---|
| OTA (firmware `OTA_URL`) | `http://<LAN_IP>:8003/xiaozhi/ota/` |
| WebSocket (returned by OTA) | `ws://<LAN_IP>:8000/xiaozhi/v1/` |
| Vision explain | `http://<LAN_IP>:8003/mcp/vision/explain` |

`GET` on the OTA URL returns a plain-text banner; the device `POST`s JSON with `Device-Id` and `Client-Id` headers and receives `{"server_time":…,"firmware":…,"websocket":{"url":"ws://<LAN_IP>:8000/xiaozhi/v1/","token":""}}`.

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

## Adding provider code

`server/overlay/` is the only way to add or change server code. Its contents mirror `main/xiaozhi-server/` of the upstream clone (for example `server/overlay/core/providers/llm/<name>/<name>.py`) and are copied over the upstream tree at image build (`server/Dockerfile`, `__pycache__` excluded from both). Python packages the overlay needs beyond the upstream base image are pinned in `server/requirements-overlay.txt`, installed by the same Dockerfile. With the overlay empty the image equals upstream. Apply with `./scripts/setup.sh`. The vendored clone is never edited.

### Tests

`server/tests/conftest.py` puts `server/overlay` before the vendored `main/xiaozhi-server` on `sys.path` (override the clone location with `WEIXUE_VENDOR`), stubs `config.logger`, `core.handle.receiveAudioHandle` and `core.handle.reportHandle`, and runs every test from a scratch directory whose `core/` links to the overlay. Mocked by default; `RUN_LIVE_TESTS=1` adds the real-API smoke tests.

```bash
uv venv --python 3.10 .venv && uv pip install --python .venv/bin/python -r server/requirements-test.txt
. .venv/bin/activate && cd server
python -m pytest                                   # mocked, no network
RUN_LIVE_TESTS=1 GOOGLE_APPLICATION_CREDENTIALS=<sa.json> ANTHROPIC_API_KEY=... python -m pytest -s -k live
```

`opuslib_next` needs `libopus` and `pydub` needs `ffmpeg`. Without a system copy, place `libopus.dylib` in `.venv/lib/` (the conftest adds it to `DYLD_FALLBACK_LIBRARY_PATH`) and `ffmpeg` in `.venv/bin/`.

## Google Cloud credentials

`GOOGLE_SA_FILE` in `server/.env` is the host path of the service-account JSON; compose mounts it read-only at `/run/secrets/google-sa.json`, the `credentials_file` of both Google providers. The key's project needs the Speech-to-Text and Text-to-Speech APIs enabled.

## Google Cloud STT (`ASR.GoogleSTT`, `type: google_stt`)

`server/overlay/core/providers/asr/google_stt.py`: Speech-to-Text v2, model `chirp_3`, location `us`, synchronous `Recognize` on the 16 kHz mono PCM the server already produces, `language_codes: ["en-US", "cmn-Hans-CN"]` (one retry with `["auto"]` if the list is rejected). The live test sends the two 16 kHz clips in `tests/assets/` and expects "turn on the living room lights" in English and Mandarin.

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
cp .env.example .env     # set LAN_IP, TZ/TZ_OFFSET, ANTHROPIC_API_KEY, GOOGLE_SA_FILE (chmod 600)
./scripts/setup.sh
```

`setup.sh` is idempotent. In order: downloads SenseVoiceSmall (~936 MB) if missing, checks the vendor clone, starts `mcp-endpoint-server`, renders `config/config.template.yaml` to `data/.config.yaml` with `scripts/render_config.py` (values are escaped; `@@NAME?@@` is optional and renders empty when unset), then builds and force-recreates the server container. Logs: `docker logs -f xiaozhi-esp32-server`.

Providers: VAD SileroVAD, ASR `GoogleSTT`, LLM `AnthropicLLM`, TTS `GoogleTTS`. Upstream `FunASR` (SenseVoiceSmall, local), `EdgeTTS` and the OpenAI-compatible `StopgapLLM` (Groq, `LLM_*` in `.env`) stay configured and are selected by editing `selected_module`. `wakeup_words` adds `Jarvis` to the upstream list.

Optional web console (MySQL + Redis + manager-web, port 8002): set `MYSQL_ROOT_PASSWORD` in `.env`, then `docker compose --profile console up -d`. Not validated.

## Verification

- `curl http://<LAN_IP>:8003/xiaozhi/ota/` returns the OTA banner; `POST` returns the JSON above.
- Port 8000 accepts WebSocket; a `hello` returns `session_id`; a `listen/detect` text turn returns `stt`, `tts start/sentence_start/stop` and Opus frames.
- Not verified without hardware: ASR on live microphone audio.

## Migrate to Ubuntu

1. Install Docker Engine + compose plugin; clone this repo; clone both repos from `vendor.lock` at the pinned commits into `~/Projects/weixue-vendor/`.
2. Copy `server/.env` (secrets, 0600) and `server/data/` (`.config.yaml`, `mcp-endpoint/` key, `models/`). Copying `mcp-endpoint/` keeps MCP URLs valid; omit `models/` to let `setup.sh` re-download.
3. Copy the Google service-account JSON (0600) and point `GOOGLE_SA_FILE` at it.
4. Set `LAN_IP` in `.env` to the Ubuntu host's address and run `./scripts/setup.sh`.
5. Re-point the firmware `OTA_URL` to the new address, or give the host the old IP / a DNS name. MCP clients on other hosts need an SSH tunnel or a changed 8004 binding.
6. Open TCP 8000 and 8003 on the host firewall.
