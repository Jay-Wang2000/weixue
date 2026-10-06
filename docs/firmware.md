# Firmware (feat-firmware-base)

xiaozhi-esp32 2.5.1 (vendored clone pinned in `vendor.lock`) built for the Waveshare ESP32-S3-Touch-AMOLED-1.75C as build entry `esp32-s3-touch-amoled-1.75c-weixue`. The vendored clone is never edited; everything weixue-specific is in `firmware/`, and source changes to upstream files are patches in `firmware/patches/`.

| File | Role |
|---|---|
| `firmware/board/config.weixue.json` | Build entry: 1.75C board symbol, device AEC, `CONFIG_RECEIVE_CUSTOM_MESSAGE`, `CONFIG_OTA_URL`, 32 MB flash + `partitions/v2/32m.csv` |
| `firmware/dependencies.lock` | Pinned managed components (esp-sr 2.4.7, esp-wifi-connect 3.3.1, LVGL 9.5.0, ...) |
| `firmware/build.sh` | Builds in a scratch copy (`$WEIXUE_FW_WORK`, default `~/Projects/weixue-vendor/fw-work/<checkout dir name>`) with `--language en-US --wake-word wn9_jarvis_tts`; asserts the resulting sdkconfig and writes `weixue-build.sha256` (sdkconfig, `config.weixue.json`, `build/flash_args` and every file it lists) |
| `firmware/patches/*.patch` | Source changes to upstream files, applied to the scratch copy on every build (see Source patches) |
| `firmware/flash.sh` | Checks port, the scratch copy against `weixue-build.sha256`, that it matches `config.weixue.json` (file and `CONFIG_OTA_URL`), `esptool flash-id` (32 MB), and a sha256-verified full 32 MB backup of this device's MAC, then `esptool write-flash @build/flash_args` |
| `scripts/venv.sh` | Creates or repairs the project `.venv` with the `vendor.lock` esptool pin (5.4.0); `flash.sh` and `scripts/backup_flash.sh` use only this esptool |
| `firmware/custom-msg-probe.sh`, `firmware/tools/custom_msg_probe.py` | Test proxy for the on-device `custom` message check (see below) |

## Build

```bash
firmware/build.sh            # prints build_seconds, binary sizes, the manifest and work_dir
```

Each checkout (main or a task worktree) builds in its own scratch copy, `~/Projects/weixue-vendor/fw-work/<checkout dir name>` (for example `fw-work/weixue-wt-feat-firmware-base`), so parallel tasks never share build state. The first build of a scratch copy is a full build and downloads managed components from components.espressif.com. The mirror keeps the generated `main/assets/lang_config.h`; ninja regenerates it when the language or its `language.json` changes. A `dependencies.lock differs` warning means `firmware/dependencies.lock` is stale; after an intentional component update, copy `<work_dir>/dependencies.lock` to `firmware/dependencies.lock` and commit it. Changing the backend address means editing `CONFIG_OTA_URL` in `firmware/board/config.weixue.json` and rebuilding.

## Source patches

`build.sh` mirrors the vendored clone into the scratch copy with `rsync --delete`, so edits made in the scratch copy are lost on the next build. Changes to upstream sources (board `.cc`, display classes, new drivers) are `-p1` unified diffs in `firmware/patches/`, applied in name order with `git apply` after the mirror; the build fails when a patch no longer applies to the `vendor.lock` commit. To create one, edit a file in the scratch copy after a build, then diff it against the clone:

```bash
cd ~/Projects/weixue-vendor
diff -uN xiaozhi-esp32/main/<path> fw-work/<checkout dir name>/main/<path> > <checkout>/firmware/patches/NN-<name>.patch
```

A diff taken after a build includes the hunks of patches already applied to that file; keep each file's changes in one patch.

## Flash

`build.sh` sources `scripts/env.sh`; `flash.sh` and `backup_flash.sh` use `.venv/bin/esptool` (created on first use). The manual `idf.py` commands below need `source scripts/env.sh` first (see `docs/toolchain.md`).

```bash
source scripts/env.sh
ls /dev/cu.usbmodem*         # one port expected
scripts/backup_flash.sh      # if no full backup of this device exists yet
firmware/flash.sh [port]
idf.py -C ~/Projects/weixue-vendor/fw-work/<checkout dir name> -p <port> monitor   # Ctrl-] to exit
```

`flash.sh` refuses to flash unless `shasum -a 256 -c weixue-build.sha256` passes in the scratch copy, so it writes exactly the image `build.sh` verified and never rebuilds; it writes bootloader (0x0), partition table (0x8000), otadata (0x10d000), app (0x200000) and assets (0xa00000) at 460800 baud. NVS (0x9000 `nvsfactory`, 0x3b000 `nvs`) keeps the same offsets as the stock 32 MB layout and is not erased, so stored Wi-Fi credentials survive. `backup_flash.sh` stores the device MAC (`flash-<timestamp>.mac`) and the backup's sha256 (`flash-<timestamp>.sha256`) next to each backup; `flash.sh` prefers a backup tagged with the connected device's MAC, warns when only untagged backups exist, and stops when the selected backup has no `.sha256` or fails `shasum -a 256 -c` (`backup/flash-20261006-163547.bin` predates both). `idf.py -C <work> -p <port> erase-flash` resets everything; restore the stock image with `.venv/bin/esptool --chip esp32s3 --port <port> write-flash 0 backup/flash-<timestamp>.bin` after `shasum -a 256 -c` of its `.sha256`.

## Device behaviour

- OTA: POST `CONFIG_OTA_URL` (NVS `wifi/ota_url` overrides it when non-empty; the Wi-Fi portal's Advanced tab sets it, an empty submit writes `""` and restores the default). The response's `websocket.url` is where the device streams audio. While the server is unreachable the device retries 10 times with doubling delays (about 85 min).
- Wake word: the ESP-SR model `wn9_jarvis_tts` detects "Jarvis" and sends `{"type":"listen","state":"detect","text":"Jarvis"}`. Exactly one `AfeAudioEngine: Model 0: wn9_jarvis_tts` line appears at boot.
- Wi-Fi provisioning (no stored SSID): join the `Xiaozhi-XXXX` hotspot and follow the on-screen portal URL.
- Power (ADR-12, `firmware/patches/05-board-power-usb.patch`): after 60 s idle the screen dims to 20 %. After `CONFIG_WEIXUE_SCREEN_OFF_SECONDS` idle (default 300, `0` = never), on USB and battery, the panel turns off (CO5300 DISPOFF, brightness 0) and logs `Panel off after 300 s idle (VBUS present|absent)`; MCU, Wi-Fi and wake word detection keep running. Wake word, touch (LVGL `LV_EVENT_PRESSED`), any `SetPowerSaveLevel` above low power (session start, notification, upgrade) or the device leaving idle (checked every second) turns it back on (`Panel on`) and restores the dim. Idle PMIC power-off happens only while the AXP2101 reports no VBUS, after `CONFIG_WEIXUE_BATTERY_IDLE_POWER_OFF_SECONDS` (default 300, `0` = never); set either in `config.weixue.json` `sdkconfig_append`. On USB the board logs `Idle power-off skipped: VBUS present` once per idle period; unplugging USB during that period wakes the screen and restarts the battery countdown. Boot logs `VBUS present|absent, battery present|absent, idle power-off on battery after N s, panel off after N s`. USB idle check: `docs/logs/fix-power-usb-idle-usb-2026-10-06.log` (300 s) and `docs/logs/fix-power-usb-idle-usb-test90-2026-10-06.log` (test build with 90 s). Panel-off check: `docs/logs/fix-screen-off-device-2026-10-06.log` (see Panel off below).
- Device AEC is on; listening mode is realtime (barge-in). BOOT double-click toggles AEC until reboot.
- Custom messages: `CONFIG_RECEIVE_CUSTOM_MESSAGE=y`. A server message `{"type":"custom","payload":{...}}` logs `Received custom message:` and shows the payload as a system chat line; a non-object payload logs `Invalid custom message format`; an unknown `type` logs `Unknown message type`.
- Audio: the device decodes 16 kHz server audio and resamples to its 24 kHz output; each session logs `Server sample rate 16000 does not match device output sample rate 24000, resampling may cause distortion`.
- OTA firmware updates: the device reports `board.type` = `esp32-s3-touch-amoled-1.75` (the board directory, not the weixue build name), and the server offers any `server/data/bin/esp32-s3-touch-amoled-1.75_<version>.bin` newer than 2.5.1, which would replace this firmware. Keep `server/data/bin` free of such files; self-hosted OTA updates need a weixue-specific board type first.
- Network constraint: the device must reach the backend host on the LAN. On a Mac running Cisco Secure Client, `192.168.8.0/24` routes into the VPN tunnel while it is connected and the device's OTA connect to `:8003` fails with `connect_failed, native=113`; device sessions need the VPN disconnected or local LAN access allowed.

## Custom message check

`firmware/custom-msg-probe.sh` runs a proxy on port 8013 (Docker, backend image). It forwards OTA to the backend with `websocket.url` rewritten to itself, relays each WebSocket session, and after the server `hello` sends `{"type":"custom","payload":{"probe":"weixue","check":"valid"}}`, `{"type":"custom","payload":"not-an-object"}` and `{"type":"weixue_probe_unknown"}`.

1. `firmware/custom-msg-probe.sh` (leave running).
2. Point the device at it: press BOOT within the first seconds after reset (device state `starting`) to open the Wi-Fi portal, join the `Xiaozhi-XXXX` hotspot, Advanced tab, OTA URL `http://192.168.8.220:8013/xiaozhi/ota/`, save, reboot.
3. Start a session ("Jarvis") and finish one round. Expected serial lines: `Received custom message:` with the valid payload (shown as a system chat line on screen), `Invalid custom message format: missing payload`, `Unknown message type: weixue_probe_unknown`; the round's STT, reply and TTS proceed normally.
4. Restore: portal Advanced tab, empty OTA URL, save, reboot; boot log shows OTA to `:8003` again.

## On-device results (2026-10-06)

Logs (SSID/BSSID and tokens redacted): `docs/logs/firmware-base-flash-2026-10-06.log` (`flash.sh` output, progress bars removed), `docs/logs/firmware-base-boot-2026-10-06-flashsh.log` and `firmware-base-boot-server-2026-10-06-flashsh.log` (120 s from reset after `flash.sh`, 20:11 UTC), `docs/logs/firmware-base-wake-2026-10-06-flashsh.log` and `firmware-base-wake-server-2026-10-06-flashsh.log` (90 s from reset with Mac `say "Jarvis"`, 20:13 UTC), `docs/logs/firmware-base-device-2026-10-06.log` (serial, 120 s from reset), `docs/logs/firmware-base-server-2026-10-06.log` (`xiaozhi-esp32-server`, same window), `docs/logs/firmware-base-boot-2026-10-06-rebased.log` (second 120 s capture of the same build, 18:41 UTC), `docs/logs/firmware-base-wake-server-2026-10-06.log` (server side of the wake-started session, 18:06:45 UTC). Server log times are UTC-7.

| Step | Result |
|---|---|
| `build.sh` | 191 s (reconfigure + rebuild after adding `CONFIG_RECEIVE_CUSTOM_MESSAGE`); 121 s and 120 s no-change rebuilds (the second with the patch step, no patches); sdkconfig asserts pass; xiaozhi.bin 2,885,808 B in every build |
| `esptool flash-id` | ESP32-S3 (QFN56) rev v0.2, embedded PSRAM 8 MB, `Detected flash size: 32MB`. Stock backup (`backup/flash-20261006-163547.bin`, 33,554,432 B) has the same `nvsfactory`/`nvs`/`otadata` offsets and its `storage` partition ends at 0x1FF0000, the basis for flashing without erase |
| `backup_flash.sh` | 263 s read of 33,554,432 B to `backup/flash-20261006-210524.bin` with `.mac` (7c:0c:5f:42:4b:fc) and `.sha256` sidecars |
| `flash.sh` | 64 s: scratch copy passes `weixue-build.sha256`, flash-id 32MB, selects the MAC-tagged backup and passes its `shasum -c`, then `.venv` esptool 5.4.0 `write-flash @flash_args` (bootloader, partition table, otadata, assets 18.8 s, app 20.1 s), every region hash-verified. Earlier `idf.py flash` revision: 48 s |
| Boot after `flash.sh` (20:11 UTC, 120 s) | App 2.5.1, SKU `esp32-s3-touch-amoled-1.75c-weixue`; stored Wi-Fi reused, IP 192.168.8.148 at 3.7 s; OTA to `:8003` at 3.9 s: `No mqtt section found !`, `Current is the latest version`, `Activation done` (server: WebSocket config issued, `固件已是最新: 2.5.1`); exactly one `AfeAudioEngine: Model 0: wn9_jarvis_tts`; `activating -> idle` at 4.4 s, no alert; power save at 65 s; no session opened (no wake event) |
| Wake after `flash.sh` (20:13 UTC, 90 s) | Mac `say "Jarvis"` three times between 15 s and 60 s: one `Application: Wake word detected: Jarvis` at 63.4 s, `idle -> connecting`, WebSocket handshake 200 ms, `connecting -> listening -> speaking -> listening`; server logged the `hello` (features `mcp`, `glyph_push`), `listen` `detect` `Jarvis`, the wake reply, `listen start realtime` and 5 device MCP tools. The server's wake reply is the bundled `config/assets/wakeup_words_short.wav` with text `我在这里哦！` |
| Boot | App 2.5.1, SKU `esp32-s3-touch-amoled-1.75c-weixue`, srmodels loaded |
| Wi-Fi | Stored credentials reused (no portal); IP 192.168.8.148 at 4.0 s, MAC 7c:0c:5f:42:4b:fc, RSSI -83 dBm |
| OTA | POST to `:8003` 200 at 5.7 s: `No mqtt section found !`, `Current is the latest version`, `Activation done`; server log "未配置MQTT网关，为设备 7c:0c:5f:42:4b:fc 下发WebSocket配置" |
| Wake word model | Exactly one `AfeAudioEngine: Model 0: wn9_jarvis_tts`; WakeNet `wakenet9l_tts1h8_Jarvis_3_0.627_0.632`; AFE pipeline AEC -> VAD -> WakeNet |
| Idle | `activating -> idle` at 5.7 s, no alert |
| WebSocket | Session started without a `Wake word detected` line (BOOT click path): handshake 150 ms to `ws://192.168.8.220:8000/xiaozhi/v1/`, server logged the `hello`, `listen start realtime` and device MCP tool list |
| Voice round | Google STT (en-US, 1.2-1.6 s) -> Anthropic LLM -> Google TTS (first audio 0.16-0.25 s) played on the device; the session closed 10 s after the last reply (`EspTcp: TCP receive failed: -1`, device back to idle) |
| Second boot capture | OTA 200 at 4.8 s (server: WebSocket config issued, `固件已是最新: 2.5.1`), exactly one `Model 0: wn9_jarvis_tts`, `activating -> idle` at 4.8 s, no alert. A BOOT-started session at 108 s completed the handshake (150 ms) and server `hello` (session id received, `listen start realtime`), then closed after 1 s with `EspTcp: TCP receive failed: -1`; the log does not show whether BOOT was pressed again |
| Third boot capture (19:05 UTC, VPN disconnected, 180 s) | RSSI -85 dBm; OTA 200 at 4.6 s (server: WebSocket config issued, `固件已是最新: 2.5.1`), exactly one `Model 0: wn9_jarvis_tts`, `activating -> idle` at 4.6 s, no alert. A session started at 6.4 s without a `Wake word detected` line (button), ran seven turns (STT, Anthropic with tool calls, TTS), closed at 86 s with `EspTcp: TCP receive failed: -1`, back to idle; power save at 146 s |
| Wake word detection | One on-device detection, outside any serial capture: at 18:06:45 UTC the device (`device-id` 7c:0c:5f:42:4b:fc, firmware hello with `mcp`/`glyph_push`) opened a session with `{"type":"listen","state":"detect","text":"Jarvis"}` and completed a round (Google STT 1.7 s, Anthropic, Google TTS first audio 0.13 s). 0 `Wake word detected` lines in any serial capture. Jarvis x10 not run |
| Link | Mac to device ping RTT 80-902 ms (avg 401 ms, 3 pings) at RSSI -85 dBm; 612-741 ms at -83 dBm in an earlier check; 858-1922 ms (avg 1236 ms, 3 pings) at 20:05 UTC |
| `custom` message on device | Not run. The probe needs the device's OTA URL set to `:8013` through the Wi-Fi portal (BOOT press after reset, join the hotspot, Advanced tab), a manual step. The live server has no route that injects a frame into an open session (HTTP routes: `/xiaozhi/ota/`, `/mcp/vision/explain`) |

## On-device acceptance

Human voice test, 2026-10-06, device on LAN (VPN off), live backend. Serial capture 318 lines from reset (about 5 min); server log of the same window. Raw logs are not committed.

| Check | Result |
|---|---|
| Wake detections | 2 on-device `Application: Wake word detected: Jarvis (state: 3)` events (16.5 s, 142.4 s), each followed by `idle -> connecting -> listening` and a server `listen detect "Jarvis"`. The attempt count is not in the logs, so the wake ratio against 10 attempts is not measurable; 2 of 2 observed attempts woke. DESIGN's 9/10 wake criterion is not demonstrated |
| Completed rounds | 2. Session 1: STT `拜拜。` (Google STT picked `cmn-Hans-CN`) -> LLM `handle_exit_intent` -> TTS goodbye -> session closed. Session 2: STT `What time is it?` (en-US, 1.5 s) -> Anthropic -> TTS "Its one twenty in the afternoon" (first audio 0.27 s) -> back to listening |
| "Jarvis" not answered by the LLM | Pass: each wake produced the canned `wakeup_words` reply (`我认真听着呢，请讲。`, `我在这里，等候您的指令。`) and no `大模型收到用户消息: Jarvis` line |
| Time question | Pass: spoken time reply, played on device |
| Session drop after replies | Not reproduced: 0 `TCP receive failed` lines. Session 2 stayed open 120 s after the reply until the server's idle goodbye (277 s), then `Websocket disconnected` and `listening -> idle` at 294 s. Session 1 closed on the exit intent at 35 s |
| Boot | Exactly one `AfeAudioEngine: Model 0: wn9_jarvis_tts`; `activating -> idle` at 5.1 s; no alert |
| Static | One commit ahead of `main` (4 behind: rebase before the PR); no secrets, SSIDs, `private_` URLs or `token=` values in `docs/` or `firmware/`; BSSID redacted in two flashsh logs |

Findings: wake replies are Chinese regardless of the English persona; Google STT recognised a short utterance as `cmn-Hans-CN` and triggered the exit intent; the first session needed 4.4 s from WebSocket handshake to server hello while the server was initialising a concurrent client session (the server log also holds that client's `listen detect` traffic for session `21edf001`, excluded from the counts above).

## Panel off (fix-screen-off, 2026-10-06)

USB power, live backend, Mac `say` at volume 55. Logs: `docs/logs/fix-screen-off-flash-2026-10-06.log`, `fix-screen-off-device-2026-10-06.log` (serial, port opened 9 s after the post-flash reset; times in brackets are capture seconds, `I (ms)` is device uptime), `fix-screen-off-server-2026-10-06.log` (UTC-7), `fix-screen-off-boot-2026-10-06.log` (30 s from an RTS reset).

| Check | Result |
|---|---|
| Build | `build.sh` 128 s incremental; sdkconfig has `CONFIG_WEIXUE_SCREEN_OFF_SECONDS=300`; xiaozhi.bin 2,887,248 B |
| Boot | `VBUS present, battery present, idle power-off on battery after 300 s, panel off after 300 s`; one `Model 0: wn9_jarvis_tts`; `activating -> idle` at 4.2 s |
| Dim | `Enabling power save mode`, brightness 20 at 65.4 s uptime |
| Panel off | `Backlight: Set brightness to 0` and `Panel off after 300 s idle (VBUS present)` at 306.0 s uptime |
| Wake word with panel off | `say "Jarvis"` 20 s later: `Wake word detected: Jarvis` at 330.29 s, `Exiting power save mode`, `Panel on` at 330.31 s |
| Round after relight | STT `What time is it?` (en-US, 1.8 s) -> Anthropic -> TTS "It's three thirty-two in the afternoon" played; `Goodbye` -> `handle_exit_intent` -> `listening -> idle` at 353.9 s |
| Re-arm | Dim again at 413.4 s, `Panel off after 300 s idle` at 653.0 s (299 s after idle) |
| Touch relight | Not run on device (needs a finger on the panel) |
