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

Changes to upstream functions that are too small to justify overriding a whole module go in `server/overlay/weixue_patches.py` (and `weixue_clock.py` for the OTA clock). The image's entrypoint, `weixue_main.py`, applies both and then runs upstream `app.py`. Current patches:

- `ConnectionHandler._extract_direct_answer_response` is replaced by `decode_direct_answer`: it decodes the `response` JSON string of the streamed `direct_answer` arguments (escapes, `\uXXXX`, surrogate pairs) and stops at its closing quote or at an incomplete escape, so each streamed result extends the previous one. `_clean_response_garbage` returns its input unchanged: with framing removed by the decoder, no chunk-level stripping is applied (upstream's stripped whitespace and quotes at chunk edges: "isfour", "Say 'onnow").
- `ConnectionHandler.handle_connection`, `ListenTextMessageHandler.handle` and the aiohttp app of `core.http_server` are wrapped for proactive messages (`docs/proactive.md`).
- `ListenTextMessageHandler.handle` is also wrapped for the wake cue, and `core.providers.vad.silero.VADProvider.is_vad` for the wake utterance hold (see Wake cue).
- `OTAHandler.handle_post` and `WebSocketServer._handle_auth` are wrapped for the strict device allow-list (see Network boundary).
- `ConnectionHandler.just_woken_up` is a property that always reads `False`: upstream sets it on every `listen detect` and then drops the next 2 s of microphone audio.
- `receiveAudioHandle.no_voice_close_connect`: an audio frame received while the server holds the turn (`client_is_speaking`, from the STT result or wake cue to `tts stop`) refreshes `last_activity_time`, so the idle close counts from the end of the reply (see Conversation timing).
- `McpTextMessageHandler.handle` logs every device MCP notification whose method starts with `notifications/weixue/` as `device event <method> device=<mac> params=<json>` before upstream handles it (panel decisions, `docs/firmware.md` Monitor panel; IMU gestures).
- `OTAHandler.handle_post` (`weixue_clock.py`) adds `server_time.tz` to the OTA response; the server logs `OTA server_time.tz: <tz>` at start.
- `intentHandler.checkWakeupWords` is replaced: a transcript that is only a `wakeup_words` entry (case-insensitive, punctuation ignored) is dropped with no reply. `enable_wakeup_words_response_cache` and the cached `wakeup_words` clips are therefore unused.
- `sendAudioMessage` (in `core.handle.sendAudioHandle` and `core.providers.tts.base`) is wrapped to record, for the current sentence, the audio packets sent, whether its last packet went out unaborted and whether a text segment got no audio (proactive delivery outcome, `docs/proactive.md`), and each captioned reply sentence with the times of its first and latest packet (echo guard). The Google TTS provider reports a synthesis that raised to the same record.
- `core.providers.asr.base.startToChat` is wrapped as the echo guard (see Echo guard); a transcript that passes it cancels a pending wake cue.
- `SimpleHttpServer.__init__` is wrapped so proactive device requests use its `server.auth` (`docs/proactive.md`, Device identity).

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
- The cue belongs to the turn that was current at the detect (`sentence_id`). Before it takes the connection, and after every await (clip load, `tts start`, decoder reset, audio), it stops once a newer turn owns the connection, live voice was heard, the server holds a turn or the connection closed. A new detect cancels a pending cue, as do a transcript that starts a turn and the end of the session.

The device's microphone stream is continuous from the wake word into listening (`docs/firmware.md` Device behaviour, wake capture): the audio after the wake word arrives right behind the wake-word audio, before the `listen detect`, so a short request said in one breath with the wake word ("Jarvis, what time is it?") reaches ASR whole and the cue is skipped (`docs/logs/fix-wave3-firmware-wake-trials-2026-10-07.log`).

Re-render the clips with `server/scripts/render_wake_cues.py` (usage in its docstring; runs in the server container).

### Echo guard

A transcript is dropped before upstream `startToChat` (so it neither barges in nor reaches the LLM) when its utterance started while a reply sentence was playing and it matches that sentence. Wake cues are never compared: "Yes." after the "Yes?" cue is the user's answer, and an echo of a cue reaches the LLM. "Playing" is the turn's current sentence while the server held the turn (`client_is_speaking`) at the utterance start, or a sentence whose latest packet went out less than `ECHO_TAIL_S` (1 s) before it; the start is the VAD's first voiced frame of the utterance. A transcript of fewer than 6 letters must match a whole sentence (letter similarity at least 0.8); a longer one must have at least 70 % of its words, in order, inside one sentence (Chinese: characters). A short answer that is not the played sentence ("Stop." during a reply) is kept. Constants and rules: `weixue_patches.py`, Playback record and echo guard.

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

`server/overlay/core/providers/asr/google_stt.py`: Speech-to-Text v2, model `chirp_3`, location `us`, synchronous `Recognize` on the 16 kHz mono PCM the server already produces, `language_codes: ["en-US", "cmn-Hans-CN"]`. Only an `InvalidArgument` that rejects the language list (a `language_codes` field violation, "Bad language code", or "The language … is not supported by the model") is retried once with `["auto"]`, within what is left of the same `timeout`; any other error ends the turn with no transcript. An utterance over 59 s (`Recognize` accepts at most 60 s) is not sent or trimmed: the receive buffer stops growing one frame past the limit, the provider logs a warning, skips the LLM turn and the device speaks `too_long_reply` (default, English then Mandarin: "Sorry, that was too long for me. Please say it again, a bit shorter." / "抱歉，这段话太长了，请说短一点再试一次。"). The live test sends the two 16 kHz clips in `tests/assets/` and expects "turn on the living room lights" in English and Mandarin.

## Google Cloud TTS (`TTS.GoogleTTS`, `type: google_tts`)

`server/overlay/core/providers/tts/google_tts.py`: Chirp 3 HD, one speaker for both locales (`speaker`, default `Charon`): CJK text goes to `cmn-CN-Chirp3-HD-<speaker>`, Latin text to `en-US-Chirp3-HD-<speaker>`, anything else to `default_language` (`en` | `zh`). The streaming path uses `streaming_synthesize` with raw PCM at the connection sample rate, one request per text segment, 60 ms Opus frames. Besides upstream's punctuation, a segment ends at an English sentence end (`.`/`…` followed by whitespace; abbreviations such as "Dr." and single-letter initials excluded, decimals never match), and unpunctuated text is cut at a word boundary once it exceeds 250 characters. A request is split to at most 5,000 UTF-8 bytes, preferring a sentence end, then a clause mark, then whitespace, then a character boundary. Each streaming call is cancelled within 50 ms of an abort, a newer turn (`sentence_id`) or `close()`, also while the first response is pending; a cancellation the provider did not request is logged as an error and still ends the turn; the sync path (`to_tts`, wake-word cache) uses `LINEAR16`. `speaking_rate` (0.25-2.0, or the `ttsRate` percentage) is the only streaming-path knob. A relative `credentials_file` resolves against the server directory; without one the client uses `GOOGLE_APPLICATION_CREDENTIALS`.

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

Required `.env` keys: `LAN_IP`, `BIND_IP`, `ALLOWED_DEVICES`, `TZ`, `TZ_OFFSET`, `ANTHROPIC_API_KEY`, `GOOGLE_SA_FILE`, `HA_MCP_URL` (`REQUIRED_KEYS` in `setup.sh`).

`setup.sh` is idempotent. In order: a pre-flight that fails before any build, publish or container change when a required key is unset or empty, `GOOGLE_SA_FILE` is missing, or `BIND_IP` is not accepted (Network boundary); downloads SenseVoiceSmall (~936 MB) if missing, checks the vendor clone, starts `mcp-endpoint-server`, renders `config/config.template.yaml` with `scripts/render_config.py` (values are escaped; `@@NAME?@@` is optional and renders empty when unset) to a mode-0600 temporary file next to `data/.config.yaml`, tags the current `mcp-bridge` and server images (and `weixue/wg:local` while `WG_TUNNEL=1`) `:rollback`, builds them, and validates the rendered file with `scripts/validate_config.py` inside the new server image (YAML, `server.websocket`, auth allow-list, selected modules, LLM key, `mcp_endpoint`; problems are printed without values). A failed render, build or validation exits with `data/.config.yaml` and the `:local` images unchanged. Then it renames the file over `data/.config.yaml` (the previous one is kept as `data/.config.yaml.rollback`), writes `data/mcp-bridge/endpoint-url` the same way, force-recreates the containers (`wg` too while `WG_TUNNEL=1`; otherwise it then removes the `wg` container) and waits up to 90 s for `scripts/check_ready.py` (OTA banner on 8003, WebSocket handshake and banner frame on 8000, at `BIND_IP`, or `127.0.0.1` for `0.0.0.0`). After a successful check it copies `.env` to `.env.deployed` (0600). If the recreate or the check fails, it restores the previous config and images and, when `.env.deployed` exists, `.env` from it (the attempted file is kept as `.env.failed`, 0600), recreates the containers in that file's tunnel mode and exits 1. If that second recreate also fails, the stack is down: it prints a `manual restore:` block (retag each `:rollback` image to `:local`, then `docker compose [--profile …] up -d --force-recreate …` from `server/`) and exits 1. `docker-compose.yml` is not rolled back; without `.env.deployed` (first run) neither is `.env`. Logs: `docker logs -f xiaozhi-esp32-server`.

Providers: VAD SileroVAD, ASR `GoogleSTT`, LLM `AnthropicLLM`, TTS `GoogleTTS`. Upstream `FunASR` (SenseVoiceSmall, local), `EdgeTTS` and the OpenAI-compatible `StopgapLLM` (Groq, `LLM_*` in `.env`) stay configured and are selected by editing `selected_module`. `wakeup_words` adds `Jarvis` to the upstream list.

Optional web console (MySQL + Redis + manager-web, port 8002): set `MYSQL_ROOT_PASSWORD` in `.env`, then `docker compose --profile console up -d`. Not validated.

## Verification

- `curl http://<LAN_IP>:8003/xiaozhi/ota/` returns the OTA banner; `POST` returns the OTA response (Firmware endpoints).
- Port 8000 accepts WebSocket; a `hello` returns `session_id`; a `listen/detect` text turn returns `stt`, `tts start/sentence_start/stop` and Opus frames.
- Not verified without hardware: ASR on live microphone audio.

## Office / off-LAN mode (WireGuard tunnel)

When the Mac is off the home LAN, `mcp-bridge` reaches ha-mcp through a WireGuard client sidecar, compose service `wg` (profile `tunnel`, image `weixue/wg:local` built from `server/wg/`: pinned Alpine + `wireguard-tools`, kernel WireGuard). Only the bridge shares the sidecar's network namespace (`network_mode: service:wg`); the server, `mcp-endpoint-server` and the device path are unchanged. The bridge still reaches `ws://mcp-endpoint-server:8004` by name: the sidecar sits on the project network and keeps Docker's resolver.

Prerequisites:

- The router's WireGuard server (GL.iNet) is reachable from the WAN on its UDP port.
- `server/.secrets/wg0.conf` (mode 0600, directory 0700) is the router's client config with `AllowedIPs = 192.168.8.0/24` only, so nothing but home-LAN traffic enters the tunnel. MTU 1420 (the `MTU` line, or wg-quick's default on a 1500-byte path).
- `DNS` lines in the config are ignored; `HA_MCP_URL` uses an IP address.

| Item | Behaviour |
|---|---|
| Config | Mounted read-only at `/etc/wireguard/wg0.conf` (`create_host_path: false`: a missing file fails the start instead of creating a directory). The sidecar brings `wg0` up from a copy without `DNS` lines on a tmpfs |
| Start | A failed `wg-quick up` (for example the endpoint name not resolving at boot) is retried every 10 s with one redacted log line per failure; the container keeps running |
| Keepalive | Peers without `PersistentKeepalive` get 25 s, so the sidecar handshakes without bridge traffic |
| Healthcheck | Healthy while the latest handshake is under 180 s old; 60 s start period |
| Logs | `docker logs wg` prints one line (`wg0 up, N peer(s), mtu M`); wg-quick errors are printed with addresses, keys, endpoints and quoted values redacted |
| Bridge | In both modes exits non-zero after `ENDPOINT_MAX_FAILURES` consecutive failed endpoint connects (`mcp-bridge/weixue_bridge/config.py`), and when its `eth0` disappears; `restart: unless-stopped` starts it again, in tunnel mode inside the sidecar's current namespace |

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
