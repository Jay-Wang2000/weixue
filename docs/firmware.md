# Firmware (feat-firmware-base)

xiaozhi-esp32 2.5.1 (vendored clone pinned in `vendor.lock`) built for the Waveshare ESP32-S3-Touch-AMOLED-1.75C as build entry `esp32-s3-touch-amoled-1.75c-weixue`. The vendored clone is never edited; everything weixue-specific is in `firmware/`: new source files are in `firmware/overrides/`, changes to upstream files are patches in `firmware/patches/`.

| File | Role |
|---|---|
| `firmware/board/config.weixue.json` | Build entry: 1.75C board symbol, device AEC, `CONFIG_RECEIVE_CUSTOM_MESSAGE`, `CONFIG_OTA_URL`, 32 MB flash + `partitions/v2/32m.csv` |
| `firmware/dependencies.lock` | Pinned managed components (esp-sr 2.4.7, esp-wifi-connect 3.3.1, LVGL 9.5.0, ...) |
| `firmware/build.sh` | Builds in a scratch copy (`$WEIXUE_FW_WORK`, default `~/Projects/weixue-vendor/fw-work/<checkout dir name>`) with `--language en-US --wake-word wn9_jarvis_tts`; asserts the resulting sdkconfig and writes `weixue-build.sha256` (sdkconfig, `config.weixue.json`, `build/flash_args` and every file it lists) |
| `firmware/overrides/` | New files copied into the scratch copy at the same path on every build; `build.sh` stops when one would replace an upstream file |
| `firmware/patches/*.patch` | Source changes to upstream files, applied to the scratch copy on every build (see Source patches) |
| `firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.{h,cc}`, `firmware/patches/11-face-display.patch` | Procedural eyes face (see Face) |
| `voice_ring.h`, `bedside_schedule.h` (same directory), `firmware/patches/21-ring-bedside.patch` | Voice-state ring and bedside page (see Ring and bedside page) |
| `firmware/tests/run.sh` | Host build and run of the tests for `firmware/overrides` sources (`imu_gestures_test.cc`, `ring_bedside_test.cc`) |
| `firmware/tools/imu_gesture_test.py` | On-device IMU gesture check over the serial console (see IMU gestures) |
| `firmware/flash.sh` | Checks port, the scratch copy against `weixue-build.sha256`, that it matches `config.weixue.json` (file and `CONFIG_OTA_URL`), `esptool flash-id` (32 MB), and a sha256-verified full 32 MB backup of this device's MAC, then `esptool write-flash @build/flash_args` |
| `scripts/venv.sh` | Creates or repairs the project `.venv` with the `vendor.lock` esptool pin (5.4.0); `flash.sh` and `scripts/backup_flash.sh` use only this esptool |
| `firmware/custom-msg-probe.sh`, `firmware/tools/custom_msg_probe.py` | Test proxy for the on-device `custom` message check (see below) |
| `firmware/tools/serial_log.py` | Captures the serial console to a file for N seconds (`--reset` pulses RTS first); run with `.venv/bin/python` |
| `firmware/tools/face_dump.py` | Decodes the `FACEDUMP` screen dumps of a diag build's serial log into PNGs |

## Build

```bash
firmware/build.sh            # prints build_seconds, binary sizes, the manifest and work_dir
```

Each checkout (main or a task worktree) builds in its own scratch copy, `~/Projects/weixue-vendor/fw-work/<checkout dir name>` (for example `fw-work/weixue-wt-feat-firmware-base`), so parallel tasks never share build state. The first build of a scratch copy is a full build and downloads managed components from components.espressif.com. The mirror keeps the generated `main/assets/lang_config.h`; ninja regenerates it when the language or its `language.json` changes. A `dependencies.lock differs` warning means `firmware/dependencies.lock` is stale; after an intentional component update, copy `<work_dir>/dependencies.lock` to `firmware/dependencies.lock` and commit it. Changing the backend address means editing `CONFIG_OTA_URL` in `firmware/board/config.weixue.json` and rebuilding.

`WEIXUE_FACE_DIAG=1 firmware/build.sh` builds the face diagnostics in (see Face); without it `build.sh` builds them out. The flag is written to the scratch copy's `face_diag.h`, so switching it recompiles only the face sources.

## Source patches

`build.sh` mirrors the vendored clone into the scratch copy with `rsync --delete`, so edits made in the scratch copy are lost on the next build. New files live in `firmware/overrides/` under their path in the clone; `build.sh` stops if an override path exists upstream. Changes to upstream sources (board `.cc`, display classes, `application.cc`) are `-p1` unified diffs in `firmware/patches/`, applied in name order with `git apply` after the mirror; the build fails when a patch no longer applies to the `vendor.lock` commit. To create one, edit a file in the scratch copy after a build, then diff it against the clone:

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

## Face

`FaceDisplay` (board display class base, `11-face-display.patch`) hides the upstream emoji box and draws two pupil-less cyan (`#00E0FF`) rounded eyes on black with LVGL draw primitives (`lv_draw_fill` for the eye, the happy arch and the flat part of a lid, one `lv_draw_triangle` for a lid slant) in each eye object's `LV_EVENT_DRAW_MAIN`. A 33 ms LVGL timer (100 ms while nothing moves in idle/listening) eases the shape toward the target pose and only invalidates an eye whose integer box changed. The theme is forced to dark.

| Mode | Device state | Pose |
|---|---|---|
| idle | idle, starting, activating, Wi-Fi config, upgrading, other | 100x116 eyes; gaze drifts to a random point within ±22/±12 px (35% back to centre) every 1.8-5.5 s |
| listening | connecting, listening | 112x146, taller (156) while VAD reports voice; gaze near centre |
| thinking | listening or speaking after the server's `stt` text, until the first assistant sentence, idle or 20 s | 108x54 raised and looking up-right, three pulsing dots; no blink |
| speaking | speaking, notifying | 106x122 shaped by the mood, height pulsing with a 3.3 Hz + 5.1 Hz talk wave |

Mood comes from `SetEmotion` (server `llm` emotion, alerts, power save `sleepy`) and shapes idle, listening and speaking: neutral; happy (`happy funny laughing loving relaxed silly winking cool delicious kissy confident`, arch cut from the bottom); sad (`sad crying angry`, outer lids drop); surprised (`surprised shocked`, larger and rounder); sleepy (`sleepy`, half-closed); confused (`confused thinking embarrassed warning cancel cloud_off`, asymmetric). Anything else is neutral. The live backend sends an `llm` emotion (`happy` when the text has no emoji) only for replies streamed as plain text; `direct_answer` tool replies send none and leave the mood neutral.

- Blink: every 3-6 s, 18% of them double; 70 ms close, 40 ms hold, 120 ms open (x1.6 when sleepy).
- Burn-in: the eyes, thinking dots, top bar, status bar and caption move one pixel along an 8-step ring every 60 s; the screen is not scrollable and shows no scrollbar.
- Caption: the upstream chat label (single line, circular scroll), restyled to a 340 px transparent bar 128 px below centre in `#9FB4BC`; it shows assistant `sentence_start` text and system messages. `user` (STT) text clears it and switches to thinking; the next assistant sentence ends thinking. Top-bar icons and the status/clock label are `#6E8A92`.
- Serial: every mode or mood change logs `FaceDisplay: mode=<mode> mood=<mood>`.
- Diag build (`WEIXUE_FACE_DIAG=1`): every 5 s `diag fps=... frame_ms_avg=... frame_ms_max=... voice_flips=...` (rendered LVGL refreshes per second; refresh start to ready, including flush; VAD changes seen by the face); `diag blink`, `diag gaze`, `diag shift` events; 0.8 s after the first occurrence of each mode+mood pair since boot and every 60 s once idle for 5 s, a full-screen `lv_snapshot` logs `diag lit=<screen>% <circle>% ... snap_ms=...` and queues the run-length encoded screen; the `face_diag` task prints it as `FACEDUMP` lines at one line per 20 ms, without the display lock. Each snapshot holds the display lock for about 0.3 s. `.venv/bin/python firmware/tools/face_dump.py <log> <dir>` writes one PNG per dump.

## Ring and bedside page

Sources: `voice_ring.h` (ring state and invalidation tiles), `bedside_schedule.h` (night window and hint), the ring, clock and page switching in `face_display.cc`, and `firmware/patches/21-ring-bedside.patch` (`AudioService::GetOutputLevel`, `Application::GetSessionOrigin`, the `urgent` field of the proactive poll, touch and MCP tool on the board, `CONFIG_WEIXUE_BEDSIDE_START`/`END`).

Ring: a 9 px band (2% of the 466 px diameter) whose outer edge is 1 px inside the panel edge, drawn above everything from the face timer: a solid ring is one A8 coverage mask (466x466, PSRAM, built at boot) blended in the ring colour, the spin is `lv_draw_arc` steps each clipped to its own sector box. The ring is split into 20 tiles of 18° and only tiles whose content changed are invalidated; a full repaint is about 56k px, so a solid ring repaints at most every 100 ms and the spin every 66 ms (a new style or colour at once).

| Face mode | Ring |
|---|---|
| idle | off |
| listening | cyan, opacity pulsing between 64 and 224 in 6 levels at 0.8 Hz |
| thinking | 120° arc rotating clockwise at 180°/s, fading from a cyan head to a blue tail in 12 steps |
| speaking | cyan; opacity 48-224 in 32-step levels follows the RMS of the PCM frame being played (-50..-6 dBFS, 30 ms attack, 150 ms release) |
| error | red, 300 ms on / 300 ms off for 1.8 s after the status shows `Error` (network or server failure alert); continuous in the fatal-error state |

Bedside page: a seven-segment `HH:MM` clock (`#701000`, 60x112 px digits) 26 px above centre and two 84x6 px closed-eye lines (`#5A0C00`) 96 px below it; top bar, status bar and caption are hidden; the ring keeps its states in `#801400` at 55% opacity (error stays red). The page and eyes move 6 px along the 8-step burn-in ring every 60 s; `--:--` shows until the OTA response has set the clock. Serial: `FaceDisplay: bedside=on|off reason=night|day|touch|session`.

- When: during the bedside hours, local time from the OTA `server_time` (`CONFIG_WEIXUE_BEDSIDE_START` 22:00 to `CONFIG_WEIXUE_BEDSIDE_END` 07:00; equal values disable the hours), or after a `night` hint until the end hour. A `day` hint turns it off until the start hour; `auto` clears the hint.
- Interaction: a touch while the panel is on, or a session other than a non-urgent announcement, shows the face; a user transcript (`stt`, from a conversation-mode reply or a voice barge-in) or a wake word during an announcement session makes it a user session (this build detects no wake word inside a session: `CONFIG_WAKE_WORD_DETECTION_IN_LISTENING` is off and listening is realtime); the page returns 15 s after the device is idle again. A touch that turns the panel back on, IMU pick-up and face-up relight the page without leaving it. A proactive announcement with `urgent: false` plays over the page; `urgent: true` shows the face (`docs/proactive.md`).
- Screen-off: unchanged (ADR-12): the 60 s dim and the 300 s panel off apply to the page as to the face.
- Server control: device MCP tool `self.screen.set_bedside` with `mode` `night|day|auto|status` and optional `start`/`end` (`HH:MM`, both together, stored in NVS namespace `bedside` over the Kconfig defaults); it returns `{"night":..,"showing":..,"hint":..,"window":"HH:MM-HH:MM"|"off","local_time":..}` (`off`: equal, empty or malformed hours) (`night`: bedside mode is on; `showing`: the page is on screen now). The LLM calls it for requests such as "switch the screen to bedside mode".
- Diag build: the first bedside page and the first error flash each get a screen dump like a new pose, and the 60 s idle dumps cover the page.

## Device behaviour

- OTA: POST `CONFIG_OTA_URL` (NVS `wifi/ota_url` overrides it when non-empty; the Wi-Fi portal's Advanced tab sets it, an empty submit writes `""` and restores the default). The response's `websocket.url` is where the device streams audio. While the server is unreachable the device retries 10 times with doubling delays (about 85 min).
- Wake word: the ESP-SR model `wn9_jarvis_tts` detects "Jarvis" and sends `{"type":"listen","state":"detect","text":"Jarvis"}`. Exactly one `AfeAudioEngine: Model 0: wn9_jarvis_tts` line appears at boot.
- Wi-Fi provisioning (no stored SSID): join the `Xiaozhi-XXXX` hotspot and follow the on-screen portal URL.
- Power (ADR-12, `firmware/patches/05-board-power-usb.patch`): after 60 s idle the screen dims to 20 %. After `CONFIG_WEIXUE_SCREEN_OFF_SECONDS` idle (default 300, `0` = never), on USB and battery, the panel turns off (CO5300 DISPOFF, brightness 0) and logs `Panel off after 300 s idle (VBUS present|absent)`; MCU, Wi-Fi and wake word detection keep running. Wake word, touch (LVGL `LV_EVENT_PRESSED`), IMU pick-up or face-up (see IMU gestures), any `SetPowerSaveLevel` above low power (session start, notification, upgrade) or the device leaving idle (checked every second) turns it back on (`Panel on`) and restores the dim. Idle PMIC power-off happens only while the AXP2101 reports no VBUS, after `CONFIG_WEIXUE_BATTERY_IDLE_POWER_OFF_SECONDS` (default 300, `0` = never); set either in `config.weixue.json` `sdkconfig_append`. On USB the board logs `Idle power-off skipped: VBUS present` once per idle period; unplugging USB during that period wakes the screen and restarts the battery countdown. Boot logs `VBUS present|absent, battery present|absent, idle power-off on battery after N s, panel off after N s`. USB idle check: `docs/logs/fix-power-usb-idle-usb-2026-10-06.log` (300 s) and `docs/logs/fix-power-usb-idle-usb-test90-2026-10-06.log` (test build with 90 s). Panel-off check: `docs/logs/fix-screen-off-device-2026-10-06.log` (see Panel off below).
- Device AEC is on; listening mode is realtime (barge-in). BOOT double-click toggles AEC until reboot.
- Proactive poll (`firmware/patches/10-proactive-poll.patch`): while idle the device long-polls `/weixue/proactive/poll` on the OTA host and, on `200`, opens a session with `listen detect` text `weixue:proactive` (`docs/proactive.md`).
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

## IMU gestures

Sources: `firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/qmi8658.{h,cc}` (driver), `imu_gestures.h` (pick-up and face-down detection), `firmware/patches/20-board-imu-gestures.patch` (board wiring, MCP tool, mute glyph), `firmware/patches/12-application-mic-mute.patch` (`Application::SetMicMuted`).

- Driver: QMI8658A on the board's `I2C_NUM_0` bus (SDA 15 / SCL 14), probed at 0x6B then 0x6A (`WHO_AM_I` 0x05). Soft reset, accelerometer only, ±4 g at 500 Hz, little-endian, INT1/INT2 high-Z, CTRL9 handshake on `STATUSINT.bit7`. A board without the sensor boots without IMU features (`QMI8658 not found`).
- Polling: one task (`imu`, priority 2) reads the acceleration and `STATUS1` every 20 ms; when `STATUS1.TAP` is set it reads `TAP_STATUS`.
- Double-tap: the QMI8658 Tap engine (datasheet Table 35 example parameters at 500 Hz: PeakWindow 20, TapWindow 50, DTapWindow 250 samples, alpha 0.0625, gamma 0.25, PeakMagThr 0.8 g², UDMThr 0.4 g²). `TAP_NUM` 2 is a double-tap; every engine report is logged as `IMU tap: status1=… tap_status=…`. Action: `speaking` → `AbortSpeaking` (the BOOT-click path), `notifying` → `StopListening` (stops the notification).
- Pick-up: after the median-filtered acceleration (5 samples per axis) has stayed within 0.04 g for 1 s, a deviation above 0.12 g from that rest vector for 3 consecutive samples. Action: the board's display wake (as touch): leaves the 60 s dim state, turns the panel back on after the 300 s panel off and restarts both idle counts. The Any-Motion and Wake-on-Motion engines are not used: Any-Motion thresholds are per-sample slopes in 1/32 g at the 500 Hz Tap ODR, and WoM mode produces no sensor data.
- Face-down: screen-normal component below -0.8 g with |a| within 0.2 g of 1 g for 800 ms; face-up: above -0.5 g for 300 ms. The screen normal is the sensor +z axis times `kScreenNormalSign` in the board patch. Face-down calls `SetMicMuted(true)`: wake word detection and voice processing off (audio input stops after the codec power timeout), an open audio channel is closed, a playing notification stops, BOOT/`StartListening`/wake word/`WakeWordInvoke` do not start a conversation (`Muted` notification), `notify` messages are ignored, the proactive poller stops polling (the server then reports the device `offline` and holds its messages). The status bar mute slot shows the Material Symbols `mic_off` glyph and the screen shows `Muted`; face-up reverses it (`Unmuted`) and wakes the display like pick-up. The mute is software only; the microphone stays powered.
- Server visibility: every gesture logs `IMU gesture: <name> (count N)` and, while an audio channel is open, sends the MCP notification `{"jsonrpc":"2.0","method":"notifications/weixue/imu","params":{"gesture":"<name>","face_down":<bool>}}` (the server logs `收到MCP客户端请求: notifications/weixue/imu`). Device MCP tool `self.imu.get_state` returns `face_down`, `mic_muted`, `last_gesture`, `last_gesture_age_ms`, per-gesture `counts` since boot and `accel_g`.
- Host tests: `firmware/tests/run.sh` (detector scenarios: 5 min idle, tilt and straight-lift pick-ups, table double-tap, 10 pick-ups, face-down timing, 10 face-down/up cycles, 135° tilt).
- On-device check (`.venv` from `scripts/venv.sh`; opening the port resets the board, and the tool waits for the first `-> idle` state before it starts):

```bash
.venv/bin/python firmware/tools/imu_gesture_test.py --port <port> --log docs/logs/imu-<date>.log idle --seconds 300
.venv/bin/python firmware/tools/imu_gesture_test.py --port <port> --log docs/logs/imu-<date>.log trials pick_up
.venv/bin/python firmware/tools/imu_gesture_test.py --port <port> --log docs/logs/imu-<date>.log trials face_down
.venv/bin/python firmware/tools/imu_gesture_test.py --port <port> --log docs/logs/imu-<date>.log trials double_tap
```

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

## Face on-device results (feat-ui-face, 2026-10-07)

USB power, live backend, Mac `say` at volume 55. Diag build (`WEIXUE_FACE_DIAG=1`) logs: `docs/logs/ui-face-diag-device-2026-10-07.log` (serial; boot at capture start, `I (ms)` is uptime; header lists the `say` times), `ui-face-diag-server-2026-10-07.log` (UTC-7), screen dumps `docs/logs/ui-face-png/`. Release build: `ui-face-release-device-2026-10-07.log`, `ui-face-release-server-2026-10-07.log`. Builds and flashes: `ui-face-build-2026-10-07.log`, `ui-face-flash-2026-10-07.log`.

| Check | Result |
|---|---|
| Build | diag 118 s incremental, xiaozhi.bin 2,898,080 B; release 118 s, 2,895,408 B; patches 05, 10, 11 apply |
| Four states | wake `Jarvis` at 92.1 s: listening 92.18, speaking (wake reply) 92.48, listening 95.06; STT -> thinking 100.76 -> first sentence -> speaking (mood happy) 104.24; follow-ups: thinking 1.35 s and 1.50 s before speaking; `Goodbye` -> idle 171.39 s. Dumps `face-003`..`face-007` |
| Blink | 65 blinks in 330 s, 9 double; interval 3.3-6.7 s (mean 5.0 s); none while thinking |
| Gaze drift | 71 idle targets within x -19..21, y -11..9 px, 28 back to centre |
| Frame rate (5 s windows) | idle 4.0 fps mean (1.0-15.5), sleepy 4.2 (1.8-6.6), frame 9-10 ms mean, 68 ms max; listening 21.8, speaking 19.0, thinking 14.6 fps, mostly the caption's circular scroll (listening with a caption that fits: 12.7 fps) |
| Lit pixels, idle | neutral 10.55% screen / 13.43% circle (no caption), 10.50% / 13.37% (after a round); sleepy 4.90-4.97% / 6.24-6.34%. Listening 16.14% / 20.56%, speaking 12.31% / 15.68% |
| Pixel shift | `diag shift` every 60 s: (1,0), (1,1), (0,1), (-1,1), (-1,0); no scrollbar after a shift (dumps `face-002`, `face-008`) |
| Flicker | The face invalidates only an eye whose integer box changed; idle frames 9-10 ms mean; not inspected by eye (no camera on the panel) |
| Audio during diag | No `AFE(FEED)` overflow; one `Encode queue is full` frame drop at the listening snapshot (snapshot holds the display lock 306-431 ms) |
| Release smoke | No `diag`/`FACEDUMP` lines; wake, barge-in (thinking during a second STT), `What is the capital of Italy?` -> "The capital of Italy is Rome", `Goodbye` -> idle at 69.6 s |

Findings: printing dumps synchronously from the LVGL timer (first diag run, `ui-face-diag-sync-dump-2026-10-07.log`) stalled the display lock for seconds, overflowed the AFE feed ring buffer and lost the spoken question; dumps now print from the `face_diag` task. The server splits a `say` question into two STT turns in some runs (`What is the` / `of France`) and once produced a spurious `What's up, guys?` STT turn just before the question (release log).


## IMU on-device results (feat-imu, 2026-10-07)

USB power, live backend, Mac `say` at volume 55. Logs: `docs/logs/imu-build-2026-10-07.log`, `imu-flash-2026-10-07.log`, `imu-idle-device-2026-10-07.log` (420 s from an RTS reset, untouched), `imu-voice-device-2026-10-07.log` and `imu-voice-server-2026-10-07.log` (UTC-7).

| Check | Result |
|---|---|
| Build | `build.sh` 193 s; patches 05, 10, 11, 12, 20 apply; xiaozhi.bin 2,901,872 B |
| Driver init | `QMI8658 found at 0x6B`, `Reset result 0x80` at 1.96-1.99 s; `self.imu.get_state` registered; first sample `x=0.199 y=-0.536 z=0.821 g` (board resting tilted on the desk); 0 `IMU read failed` |
| Idle false positives | 0 `IMU gesture` and 0 `IMU tap` lines in 415 s idle after boot (dim at 65.4 s, `Panel off after 300 s idle (VBUS present)` at 305.0 s, `Idle power-off skipped: VBUS present`) |
| Voice round | Wake `Jarvis`, "Are you lying face down right now?" -> Anthropic called `self_imu_get_state` (`face_down:false`, `mic_muted:false`, no gestures) -> spoken answer; "What time is it?" -> time reply; `Goodbye` -> `handle_exit_intent` -> idle. 0 `IMU` gesture or tap lines during TTS playback |
| Gestures (pick-up, face-down, double-tap x10) | Not run: need a person to move the board |

## Ring and bedside on-device results (feat-ui-ring-bedside, 2026-10-07)

USB power, live backend with the branch `weixue_proactive.py` copied into the server container for runs A and B (restored after each; D-F use the main file, whose poll body has no `urgent` field, so every announcement is non-urgent), Mac `say` at volume 55, device local time UTC-7. Logs: `docs/logs/ui-ring-bedside-{d,e,f}-*` (release build after the review fixes: reply and barge-in at night, disabled window; build and flash logs `ui-ring-bedside-d-build-*`, `-d-flash-*`), `docs/logs/ui-ring-bedside-diag-a-*` (diag build, ring drawn with `lv_draw_arc`), `ui-ring-bedside-diag-b-*` (diag build, A8 mask, invalidation stats), `ui-ring-bedside-c-*` (final diag build, then release build), `ui-ring-bedside-build-2026-10-07.log`, `ui-ring-bedside-flash-2026-10-07.log` (release), screen dumps `docs/logs/ui-ring-bedside-png/`. Device logs carry the driver timeline (UTC) in their header; FACEDUMP lines are removed.

| Check | Result |
|---|---|
| Build | release 2,911,968 B (D); patches 05, 10, 11, 12, 20, 21 apply; host tests pass (`ring_bedside_test`) |
| Ring states | every round logs `ring=pulse` on wake, `ring=level` for the wake cue and replies, `ring=spin` from the STT text to the first sentence, `ring=off` at idle (A, B, C, release). Dumps `a-001-listening`, `a-003-thinking`, `a-004-speaking`, `b-002-listening-a8` |
| Error ring | server container stopped, wake word: `Alert [cancel] Error: Unable to connect to service`, `ring=error` 60 ms later, red ring in dump `a-010-error` |
| Frame time with the ring (5 s windows, face alone 13-16 ms) | arcs (A): listening 45-74 ms, thinking 48-81 ms, speaking 63-130 ms; A8 mask (B): listening 44-47 ms at 55-63k px invalidated per frame, no full-screen fallback; final (C): listening 33-38 ms, speaking 28-30 ms, 12-19 fps. No `Encode queue is full` in C |
| Night hint | "Please switch the screen to bedside mode." -> `self_screen_set_bedside` (`hint=night`) -> `bedside=on reason=night` 14.9 s after the session closed (B) |
| Window by time | "Set the bedside hours from 7:47 p.m. to 7:54 p.m., and set bedside mode to auto." -> `bedside window 19:47-19:54`; `bedside=on reason=night` at 19:47, `Panel off after 300 s idle (VBUS present)` over the page 299 s after the session, `bedside=off reason=day` at 19:54; hours then set back to 22:00-07:00 by voice (A) |
| Lit pixels, bedside page | 4.50-5.45% screen / 5.73-6.94% circle at 19:47-19:54 (A, dump `a-016-bedside`), 6.20-6.61% / 7.90-8.41% at 20:05-20:08 (B, dump `b-007-bedside`) |
| Pixel shift on the page | seven `diag shift` steps 60 s apart while the page showed (A); the dumps place the colon centre at x 239 (shift 1,0) and 227 (shift -1,1), 6 px per step |
| Relight at night | wake word: `bedside=off reason=session`, face for the round, `bedside=on` 15.0 s after idle (B); wake word with the panel off: `Panel on` (A) |
| Announcements at night | `urgent: false`: `Proactive message pending (urgent=0)`, played with the page kept (`ring=pulse bedside=1`, `ring=level bedside=1`); `urgent: true`: `urgent=1`, `bedside=off reason=session`, face for the announcement, page again 15 s later (B) |
| Reply to an announcement at night (D) | conversation message, "No, thank you." in the reply window: `>> No, thank you`, `bedside=off reason=session` 10 ms later, face for the LLM turn, page again 15.0 s after idle |
| Voice barge-in at night (F) | speech 2 s into a long announcement: playback stops, `>> Excuse me`, `bedside=off reason=session`, face for the round, page 15 s after idle; the page shows until the transcript arrives (6.6 s after playback start) |
| Wake word inside an announcement session (D, E) | "Jarvis" during playback and in the reply window: no `Wake word detected`, page kept |
| Announcement at night without speech (D) | `ring=level bedside=1` to idle, page kept |
| Disabled window (D) | "Set the bedside hours to start at 07:00 and end at 07:00" -> tool result `"window":"off"`, `night:false`; hours restored to 22:00-07:00 by voice |
| Release smoke | no `diag`/`FACEDUMP` lines; wake, "What is two plus two?" -> "Four", `Goodbye` -> idle, ring states logged (C) |
| Not run | touch on the panel and IMU pick-up/face-up relight at night (need a person); `custom`-message night hint (the hint uses the MCP tool) |
