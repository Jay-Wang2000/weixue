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

`server/overlay/` is the only way to add or change server code. Its contents mirror `main/xiaozhi-server/` of the upstream clone (for example `server/overlay/core/providers/llm/<name>/<name>.py`) and are copied over the upstream tree at image build (`server/Dockerfile`). With the overlay empty the image equals upstream. Apply with `./scripts/setup.sh` (or `docker compose up -d --build xiaozhi-esp32-server`). The vendored clone is never edited.

## Reproduce

Prerequisites: Rancher Desktop running (`~/.rd/bin` on PATH), python3, the two clones from `vendor.lock` under `~/Projects/weixue-vendor/`, ~4 GB free disk.

```bash
cd server
cp .env.example .env     # set LAN_IP, TZ/TZ_OFFSET, LLM_API_KEY (chmod 600)
./scripts/setup.sh
```

`setup.sh` is idempotent. In order: downloads SenseVoiceSmall (~936 MB) if missing, checks the vendor clone, starts `mcp-endpoint-server`, renders `config/config.template.yaml` to `data/.config.yaml` with `scripts/render_config.py` (values are escaped), then builds and force-recreates the server container. Logs: `docker logs -f xiaozhi-esp32-server`.

Providers: VAD SileroVAD, ASR FunASR/SenseVoiceSmall (local), TTS EdgeTTS (needs internet), LLM any OpenAI-compatible endpoint from `.env` (Groq `openai/gpt-oss-20b`).

Optional web console (MySQL + Redis + manager-web, port 8002): set `MYSQL_ROOT_PASSWORD` in `.env`, then `docker compose --profile console up -d`. Not validated.

## Verification

- `curl http://<LAN_IP>:8003/xiaozhi/ota/` returns the OTA banner; `POST` returns the JSON above.
- Port 8000 accepts WebSocket; a `hello` returns `session_id`; a `listen/detect` text turn returns `stt`, `tts start/sentence_start/stop` and Opus frames.
- Not verified without hardware: ASR on live microphone audio.

## Migrate to Ubuntu

1. Install Docker Engine + compose plugin; clone this repo; clone both repos from `vendor.lock` at the pinned commits into `~/Projects/weixue-vendor/`.
2. Copy `server/.env` (secrets, 0600) and `server/data/` (`.config.yaml`, `mcp-endpoint/` key, `models/`). Copying `mcp-endpoint/` keeps MCP URLs valid; omit `models/` to let `setup.sh` re-download.
3. Set `LAN_IP` in `.env` to the Ubuntu host's address and run `./scripts/setup.sh`.
4. Re-point the firmware `OTA_URL` to the new address, or give the host the old IP / a DNS name. MCP clients on other hosts need an SSH tunnel or a changed 8004 binding.
5. Open TCP 8000 and 8003 on the host firewall.
