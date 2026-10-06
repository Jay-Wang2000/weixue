# feat-firmware-base — developer brief (rev 2)

Vendored sources (read-only): `V=/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32` (commit 0d576d3d, `PROJECT_VER "2.5.1"` at `V/CMakeLists.txt:12`), `S=/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server` (repo commit 87c6df77), `I=/Users/jwang14/Projects/weixue-vendor/esp-idf` (v6.1.0). Work happens in the fork, never in `weixue-vendor`.

## 0. One-paragraph plan

Fork `78/xiaozhi-esp32`; build board dir `waveshare/esp32-s3-touch-amoled-1.75` as a **new** build entry `esp32-s3-touch-amoled-1.75c-weixue` (copy of the 1.75c entry plus `CONFIG_OTA_URL` and, once `esptool flash-id` confirms 32 MB, the 32 MB flash/partition pair); run `python scripts/build.py waveshare/esp32-s3-touch-amoled-1.75 --name esp32-s3-touch-amoled-1.75c-weixue --language en-US --wake-word wn9_jarvis_tts` from the fork root; back up flash with `esptool read-flash 0 ALL`; flash with `idf.py -p /dev/cu.usbmodem2101 flash monitor`. **Bring the server up first** (`data/.config.yaml` with `server.websocket: ws://<mac-lan-ip>:8000/xiaozhi/v1/`, `wakeup_words` containing `Jarvis`), because the device's first boot loops on the OTA POST for ~85 min if the server is down.

## 1. Toolchain

| Item | Value | Source |
|---|---|---|
| ESP-IDF required | `idf: version: '>=6.0.1'`; README: 6.0.1 minimum, 6.1 recommended, 5.x unsupported | `V/main/idf_component.yml:154-156`, `V/README.md:17,116` |
| Vendored IDF | 6.1.0: `IDF_VERSION_MAJOR 6 / MINOR 1 / PATCH 0`; checkout `fff9895c82d744c7237be8847347bdd1b07c6643`, `git describe --tags` = `v6.1` | `I/tools/cmake/version.cmake:1-5`; `git log -1`, `git describe --tags` in `I` |
| Tools location | default `~/.espressif` (`IDF_TOOLS_PATH_DEFAULT`), overridden by `IDF_TOOLS_PATH` env; export it **in every shell** before both `./install.sh esp32s3` and `. ./export.sh` | `I/tools/idf_tools.py:90,3499` |
| esptool | `requirements.core.txt:17` pins `esptool` with **no version**; install.sh pulls current release. Run `esptool version` and confirm >= 5 before relying on hyphenated `read-flash`/`flash-id` (v4 used `read_flash`/`flash_id`) | `I/tools/requirements/requirements.core.txt:17`; esptool docs https://docs.espressif.com/projects/esptool/en/latest/esp32s3/esptool/basic-commands.html (hyphenated commands, `ALL` size) |
| LVGL | `lvgl/lvgl: ~9.5.0`, `esp_lvgl_port: ~2.9.0` (comment "LVGL 9.2.2" at `sdkconfig.defaults:44` is stale) | `V/main/idf_component.yml:73-74` |
| ESP-SR | `espressif/esp-sr: ==2.4.7` | `V/main/idf_component.yml:43` |
| Wi-Fi/portal | `78/esp-wifi-connect: ~3.3.1` | `V/main/idf_component.yml:27` |
| managed_components | **absent** in the vendored checkout; first `idf.py reconfigure` (or build.py) downloads esp-sr, LVGL, esp-wifi-connect from components.espressif.com — network required. `--list-wake-words` errors until then | `ls V/managed_components` → not found; `V/scripts/build.py:37-39,223-231` |

Project rule (CLAUDE.md): no global installs → set `IDF_TOOLS_PATH` under the project; `install.sh` creates its own Python venv there.

## 2. Board identity

- Board directory: `V/main/boards/waveshare/esp32-s3-touch-amoled-1.75/` (`config.json`, `config.h`, `esp32-s3-touch-amoled-1.75.cc`, `README.md`). No `…-1.75c` directory exists; the C model is a second build entry of the same dir.
- `config.json` (`manufacturer: waveshare`, `type: esp32-s3-touch-amoled-1.75`, `target: esp32s3`) has two builds; 1.75C entry (lines 14-21): `name: esp32-s3-touch-amoled-1.75c`, `sdkconfig_append = ["CONFIG_USE_WECHAT_MESSAGE_STYLE=n", "CONFIG_BOARD_TYPE_WAVESHARE_ESP32_S3_TOUCH_AMOLED_1_75C=y", "CONFIG_USE_DEVICE_AEC=y"]`.
- Kconfig: `BOARD_TYPE_WAVESHARE_ESP32_S3_TOUCH_AMOLED_1_75C`, `depends on IDF_TARGET_ESP32S3` (`V/main/Kconfig.projbuild:542-544`).
- CMake: `elseif(CONFIG_BOARD_TYPE_WAVESHARE_ESP32_S3_TOUCH_AMOLED_1_75C)` → `BOARD_DIR "waveshare/esp32-s3-touch-amoled-1.75"`, `BUILTIN_TEXT_FONT font_noto_sans_basic_30_4`, `BUILTIN_ICON_FONT font_material_symbols_30_4`, `DEFAULT_EMOJI_COLLECTION noto-color-emoji_64` (`V/main/CMakeLists.txt:395-399`).
- C-variant pins (`config.h:19-27`): MCLK GPIO16 (1.75: 42), WS 45, BCLK 9, DIN 10, DOUT 8, LCD RST GPIO1 (39), TOUCH RST GPIO2 (40), TOUCH INT 11; PA GPIO46 (`config.h:30`). I2C bus `i2c_bus_` on `I2C_NUM_0`, SDA 15 / SCL 14 (`.cc:146,167-179`) — reuse this handle for feat-imu. TCA9554 is initialised only on the non-C board (`.cc:317-319`).
- Chips driven by the board class: CO5300 QSPI panel (`esp_lcd_co5300.h`), CST9217 touch, AXP2101 PMIC, `BoxAudioCodec` (ES8311 + ES7210 with `AUDIO_INPUT_REFERENCE`) (`.cc:3,12,22,329-341`). The 375-line board file contains **no IMU/RTC code** (grep `qmi|imu|rtc|pcf85` → 0 hits) — feat-imu starts from zero.
- Hardware per Waveshare (fetched): 1.75 wiki lists QMI8658 IMU, PCF85063 RTC, TCA9554, ES8311, AXP2101, CST9217, CO5300, 16 MB flash, 8 MB PSRAM (https://www.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-1.75). 1.75C product page lists QMI8658, ES8311 + ES7210, AXP2101, CST9217, CO5300, **"external 32MB Flash memory"**, 8 MB PSRAM, no RTC (https://www.waveshare.com/esp32-s3-touch-amoled-1.75c.htm). Upstream builds the C as 16 MB, so the 32 MB override stays conditional on `esptool flash-id`.
- Build identity: `-DBOARD_NAME=<build.name>` must match `^[a-z0-9.-]+$` (`V/main/CMakeLists.txt:1147-1151`; `build.py:107,110-119`), becomes the `BOARD_NAME` compile definition (`CMakeLists.txt:1160`) and the OTA User-Agent `<BOARD_NAME>/<version>` (`V/main/system_info.cc:53-57`).

## 3. sdkconfig: what the build must end up with

Already in tree (do not repeat): `sdkconfig.defaults` (`CONFIG_ESPTOOLPY_FLASHSIZE_16MB=y`, `partitions/v2/16m.csv`, `CONFIG_PARTITION_TABLE_OFFSET=0x8000`, lines 13-16); `sdkconfig.defaults.esp32s3` (QIO, 240 MHz, `CONFIG_SPIRAM=y`, `CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y` at line 22).

| Purpose | Symbol(s) | How |
|---|---|---|
| Wake word | `CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=n` (every `CONFIG_SR_WN_*=y` found in the two defaults files is negated, `build.py:300-310,351-352`), `CONFIG_WAKE_WORD_DISABLED=n`, `CONFIG_USE_ESP_WAKE_WORD=n`, `CONFIG_USE_CUSTOM_WAKE_WORD=n`, **`CONFIG_USE_AFE_WAKE_WORD=y`**, **`CONFIG_SR_WN_WN9_JARVIS_TTS=y`** | `--wake-word wn9_jarvis_tts` (`build.py:338-385`); validated post-reconfigure (`:1466-1483`) |
| Language | `CONFIG_LANGUAGE_EN_US=y` (Kconfig default `LANGUAGE_ZH_CN`, `Kconfig.projbuild:36-38`); `--language en-US` is validated against `main/assets/locales/*` via `_normalize_language` (`build.py:157-169`) | `--language en-US`; never bake language/wake word into `config.json` (`V/docs/custom-board.md:140-143`) |
| OTA URL | `CONFIG_OTA_URL="http://<mac-lan-ip>:8003/xiaozhi/ota/"` (default `https://api.tenclass.net/xiaozhi/ota/`, `Kconfig.projbuild:3-7`) | `sdkconfig_append` of the new build entry — build.py has **no generic -D/sdkconfig CLI**; argparse exposes only `board`, `-c`, `--list-*`, `--json`, `--name`, `--language`, `--wake-word`, `--build-options-json`, `--zip`, `--select-changed` (`build.py:1750-1800`) |
| Flash size | `CONFIG_ESPTOOLPY_FLASHSIZE_32MB=y`, `CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions/v2/32m.csv"` (pattern from `main/boards/waveshare/esp32-s3-touch-lcd-7c/config.json:10-11`) | only after `esptool flash-id` reports 32 MB |
| AEC | `CONFIG_USE_DEVICE_AEC=y` (already in entry). Preferred knob: `build_options: {"aec_mode": "device"}` in the build entry or `--build-options-json '{"aec_mode":"device"}'`; it expands to `CONFIG_USE_DEVICE_AEC=y/n`, `CONFIG_USE_SERVER_AEC=n`, `CONFIG_USE_AUDIO_PROCESSOR=y` (`build.py:577-587,621-636,722-729`). `aec_mode` is the only semantic option for this board | nothing extra required |
| Display style | `CONFIG_USE_DEFAULT_MESSAGE_STYLE` (choice default; entry forces WeChat off). `FLASH_DEFAULT_ASSETS` is the Kconfig default iff `!USE_EMOTE_MESSAGE_STYLE` (`Kconfig.projbuild:9-24`) | nothing to add |

Precedence: `SDKCONFIG_DEFAULTS` files apply in order, later wins; the target file is applied right after the file bringing it in (ESP-IDF v6.1 build-system doc, "Custom Sdkconfig Defaults" / "Target-dependent Sdkconfig Defaults", https://docs.espressif.com/projects/esp-idf/en/v6.1/esp32s3/api-guides/build-system.html; enforced at `I/tools/cmake/kconfig.cmake:187-193`). Chain = `sdkconfig.defaults` → `sdkconfig.defaults.esp32s3` → `build/xiaozhi-build.sdkconfig.defaults`, so everything in the build entry / CLI wins. Inside the fragment, `_merge_sdkconfig_options` keeps the **last** assignment per key and `_apply_auto_selects` runs after user options (`build.py:1303-1340,1645-1650`).

New build entry constraints (`build.py`): `name` must match `^[a-z0-9.-]+$` (`:107,110-119,127-129`); the explicit `CONFIG_BOARD_TYPE_…_1_75C=y` in `sdkconfig_append` must be one of the symbols whose `main/CMakeLists.txt` branch sets `BOARD_DIR "waveshare/esp32-s3-touch-amoled-1.75"` (`:1115-1152,1216-1243`); build.py prints `name: waveshare-<name>` (release artifact name via `_get_full_name`, `:132-135,147-154,1651-1654`) and `reported_name: <name>` — the latter is what goes into `-DBOARD_NAME` (`:1455-1461,1676-1681`). `build/merged-binary.bin` is always the output (`:81-96`). `custom-board.md:7-9` warns against reusing a stock identity (OTA could replace your firmware).

## 4. Wake word model: exact mechanics and pitfalls

- esp-sr `Kconfig.projbuild`: `config SR_WN_WN9_JARVIS_TTS  bool "Jarvis (wn9_jarvis_tts)"  default n` (fetched from https://raw.githubusercontent.com/espressif/esp-sr/master/Kconfig.projbuild; the skeptics report the same block with `depends on IDF_TARGET_ESP32S3 || ESP32P4 || ESP32S31` at commit 2f8c4b04 = 2.4.7). Phrase is "Jarvis", not "Hi Jarvis"; ADR-6 already allows this (`DESIGN.md:59`).
- **Exact string sent to the server is "Jarvis"**: `model/wakenet_model/wn9_jarvis_tts/_MODEL_INFO_` = `wakenet9l_tts1h8_Jarvis_3_0.627_0.632` and `get_wake_words_from_info()` in `src/model_path.c` returns the 3rd underscore token (both fetched from esp-sr `master`; same content at the 2.4.7 commit is UNVERIFIED by me — the skeptics report it). `AfeAudioEngine::Initialize` collects words via `esp_srmodel_get_wake_words()` (`afe_audio_engine.cc:99-117`), `HandleWakeNetResult` picks `wake_words_[wakenet_model_index-1]` (`:456-469`), and `Protocol::SendWakeWordDetected` sends `{"session_id":…,"type":"listen","state":"detect","text":"Jarvis"}` (`main/protocols/protocol.cc:74-79`; called from `application.cc:985`). Confirm from the server log on first wake anyway.
- Model delivery is **not** an esp-sr `model` partition: `build_default_assets.py:521-544` reads every `CONFIG_SR_WN_*=y` from `sdkconfig`, copies models from `managed_components/espressif__esp-sr/model` (`ESP_SR_MODEL_PATH`, `main/CMakeLists.txt:1201-1205,1336`), packs `srmodels.bin` into `generated_assets.bin`, flashed to `assets` (`main/CMakeLists.txt:1424-1429`, gated by `CONFIG_FLASH_DEFAULT_ASSETS`). Runtime: `Assets::LoadSrmodelsFromIndex` → `srmodel_load` → `AudioService::SetModelsList` (`main/assets.cc:110-121`). `esp_srmodel_init("model")` is only the fallback when no list is given (`afe_audio_engine.cc:66-68`). `FLASH_NONE_ASSETS` ⇒ no wake word.
- **AfeAudioEngine loads up to two WakeNet models**: every `ESP_WN_PREFIX` model is collected; `[0]` → `wakenet_model_name`, `[1]` → `wakenet_model_name_2` (`afe_audio_engine.cc:161-177`). `CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=n` (emitted by build.py) is what makes Jarvis the only model; a stray `=y` would pack a second model. `Initialize()` logs `Model %d: %s` for every packed model (`:78-80`) — acceptance is "exactly one `Model N:` line, and it is `wn9_jarvis_tts`".
- `CONFIG_SEND_WAKE_WORD_DATA` default y, `depends on USE_AFE_WAKE_WORD || USE_CUSTOM_WAKE_WORD` (`Kconfig.projbuild:950-953`).
- `--list-wake-words` reads `managed_components/espressif__esp-sr/Kconfig.projbuild`; run `idf.py reconfigure` once first (`build.py:37-39,223-231`; `custom-board.md:399-400`).
- DESIGN.md feature table still says 「你好小智」唤醒 (`DESIGN.md:40`) while ADR-6 (`:59`) says Jarvis; fix the table.

## 5. Build and flash commands

```bash
# one-time
export IDF_TOOLS_PATH=/Users/jwang14/Projects/weixue/.espressif
cd <esp-idf checkout> && ./install.sh esp32s3 && . ./export.sh
esptool version                                  # expect >= 5 (hyphenated commands)
cd <fork of xiaozhi-esp32>                       # build.py uses relative paths; run from fork root
idf.py set-target esp32s3                        # optional
idf.py reconfigure                               # downloads managed_components
python scripts/build.py --list-boards | grep -A3 amoled-1.75
python scripts/build.py --list-wake-words | grep jarvis

# build (non-interactive requires --name because the dir has >1 variant)
python scripts/build.py waveshare/esp32-s3-touch-amoled-1.75 \
  --name esp32-s3-touch-amoled-1.75c-weixue \
  --language en-US --wake-word wn9_jarvis_tts
```
What build.py does (`build.py:1428-1461,1674-1690`): moves `sdkconfig`→`sdkconfig.old`, writes `build/xiaozhi-build.sdkconfig.defaults`, runs `idf.py -DIDF_TARGET=esp32s3 -DSDKCONFIG_DEFAULTS="sdkconfig.defaults;build/xiaozhi-build.sdkconfig.defaults" -DBOARD_NAME=<name> reconfigure`, asserts the requested symbols appear in `sdkconfig` (error text "…is incompatible with this board or ESP-IDF configuration; Kconfig rejected: …", `:1479-1483`), then `idf.py build` and `merge-bin` → `build/merged-binary.bin`. Every run discards `menuconfig` edits — persistent overrides go in `config.json`.

```bash
ls /dev/cu.usbmodem*                                           # verify port
esptool -p /dev/cu.usbmodem2101 flash-id                       # real flash size
esptool -p /dev/cu.usbmodem2101 -b 921600 read-flash 0 ALL backup/flash_backup.bin
idf.py -p /dev/cu.usbmodem2101 flash monitor                   # bootloader, table, app, assets
```
Flash offsets (ESP32-S3): bootloader 0x0 (`I/components/esp_rom/esp32s3/esp_rom_caps.h:40`), partition table 0x8000 (`sdkconfig.defaults:16`). `partitions/v2/16m.csv`: nvs 0x9000/0x4000, otadata 0xd000/0x2000, phy_init 0xf000/0x1000, ota_0 0x20000/0x3f0000, ota_1 (auto → 0x410000, derived), assets 0x800000/8M. `partitions/v2/32m.csv`: nvsfactory 200K, nvs 840K, otadata 0x2000, phy_init 0x1000 (auto offsets 0x9000/0x3B000/0x10D000/0x10F000 — derived; confirm with `idf.py partition-table`), ota_0 0x200000/4M, ota_1 0x600000/4M, assets 0xA00000/16M. Switching tables moves NVS, so Wi-Fi and `ota_url` settings do not survive a layout change.

## 6. OTA → WebSocket flow (device) and server contract

Device (`main/ota.cc`, `main/application.cc`):
1. `Ota::GetCheckVersionUrl()` = NVS `wifi/ota_url` if non-empty, else `CONFIG_OTA_URL` (`ota.cc:48-55`).
2. `CheckVersion()` POSTs `Board::GetSystemInfoJson()` with `Activation-Version`, `Device-Id` (MAC), `Client-Id` (UUID), `User-Agent <BOARD_NAME>/<ver>`, `Accept-Language: <Lang::CODE>` (note for feat-server: the server may key language off this), `Content-Type: application/json` (`ota.cc:57-72,93-97`).
3. **First-boot dependency**: `CheckNewVersion` retries 10× with 10 s doubling delay (10+20+…+2560 s ≈ 85 min) showing `CHECK_NEW_VERSION_FAILED` (`application.cc:442-490`). Bring the server up and `curl http://<mac-ip>:8003/xiaozhi/ota/` before the first boot.
4. Response: `activation{code,challenge}` (`ota.cc:127-149`), `mqtt{…}` → NVS ns `mqtt` (`:152-169`), `websocket{url,token,version}` → NVS ns `websocket` (`:172-191`), `server_time` → `settimeofday` (`:194-217`), `firmware{version,url,force}` → upgrade if newer or `force==1` (`:220-246`, `application.cc:496-501`).
5. `InitializeProtocol()`: mqtt present → `MqttProtocol`; else websocket → `WebsocketProtocol`; neither → MQTT fallback (`application.cc:538-545`). No activation → skip (`:502-505`).
6. `WebsocketProtocol::OpenAudioChannel()` reads NVS `websocket/url,token,version`; a token **containing a space is sent verbatim**, otherwise `Bearer ` is prefixed; sets `Protocol-Version`, `Device-Id`, `Client-Id` (`websocket_protocol.cc:79-106`). Connection opens lazily (`:19-22`).

Server (`S`):
- Config overrides go in **`data/.config.yaml`**, never `config.yaml` (`config.yaml:1-5`); `config_loader.py:35-45` merges it over `config.yaml`, or switches to manager-api mode if `manager-api.url` is set.
- `/xiaozhi/ota/` GET/POST/OPTIONS and `/xiaozhi/ota/download/{filename}` are registered **only when `read_config_from_api` is false** (single-module mode) (`core/http_server.py:37-60`; flag set at `config_loader.py:65`). If 智控台/manager-api is enabled later, OTA moves to the Java manager and this flow no longer applies.
- Ports: `server.port` 8000 (WS), `server.http_port` 8003 (`config.yaml:9-14`); `docker-compose.yml:14-17` maps both; volumes mount `./data` and `./models/SenseVoiceSmall/model.pt` (`:18-22`) — that model file must exist locally or the container fails to start.
- `_get_websocket_url` returns `server.websocket` **verbatim whenever it does not contain "你的"** — including an empty string — else `ws://<local_ip>:8000/xiaozhi/v1/` (`ota_handler.py:125-141`). `get_local_ip()` UDP-connects to 8.8.8.8 and falls back to `127.0.0.1` (`core/utils/util.py:20-29`); in Docker it yields the container IP. Set `server.websocket: ws://<mac-lan-ip>:8000/xiaozhi/v1/` and verify with `GET /xiaozhi/ota/` (`ota_handler.py:355-363`).
- `server.mqtt_gateway` must stay `null`/empty (`config.yaml:38`); any non-empty value emits an `mqtt` section (`ota_handler.py:235-237`) and the device prefers MQTT (`application.cc:538-539`).
- Token: with `server.auth.enabled: true`, a device in `auth.allowed_devices` gets **no** token (`ota_handler.py:283-288`; `config.yaml:30-37`).
- **Auto-OTA hazard**: `handle_post` scans `data/bin/*.bin` by device model and returns `firmware.url/version` when a higher version exists (`ota_handler.py:300-330`, download via `http_server.py:54-57`). Keep `data/bin` empty (or versions ≤ 2.5.1) during bring-up.
- Wake word: `listenMessageHandler.py:61-63,97` compares `remove_punctuation_and_length(text)` against `wakeup_words` (`config.yaml:107-118`, Chinese only). Match + `enable_greeting: true` (default, `config.yaml:69`) → hard-coded greeting "嘿，你好呀" to the LLM (`:106-110`); match + `enable_greeting: false` → no reply (`:101-105`); no match → "Jarvis" is sent to the LLM as the user utterance (`:111-116`). Add `Jarvis` to `wakeup_words` (or set `enable_greeting: false`) in `data/.config.yaml`.

## 7. Settings persistence (NVS) and runtime OTA URL change

- `Settings(ns, rw)` wraps `nvs_open/nvs_get_str/nvs_set_str`, commits in the destructor (`main/settings.cc:8-47`). Namespaces relevant to this board: `wifi` (key `ota_url` read at `ota.cc:49-50`; key `sleep_mode` read by `power_save_timer.cc:32-33` and `sleep_timer.cc:38`), `websocket` (`ota.cc:175`, `websocket_protocol.cc:80`), `mqtt` (`ota.cc:154`, `mqtt_protocol.cc:80`), `board` (`board.cc:15`), `audio` (`audio_codec.cc:30,44`), `display` (`backlight.cc:34,56`, `display.cc:47`, `lcd_display.cc:81`), `assets` (`application.cc:398`, `mcp_server.cc:323`). Other boards also use `network`, `vendor`, `aec` (yunliao-s3 only), `led_strip`, `model`, `lcd_display` (grep `Settings settings("` across `main/`). **SSID/password key names are not in this repo** — they live in esp-wifi-connect's `SsidManager` (UNVERIFIED). `main.cc:17-21` erases NVS only on no-free-pages / new-version errors.
- OTA URL is changeable at runtime: `WifiBoard::StartNetwork` sets `config.show_ota_config = true` and `ssid_prefix = "Xiaozhi"` (`wifi_board.cc:56-60`); the DHCP hostname is `Xiaozhi-<MAC4><MAC5>` (`:62-68`), handy for finding the device on the router. esp-wifi-connect (GitHub `main`, fetched raw `wifi_configuration_ap.cc`): AP SSID `"%s-%02X%02X"` from `mac[4],mac[5]`; routes `GET /advanced/config` and `POST /advanced/submit`; the submit handler does `if (cJSON_IsString(ota_url) && ota_url->valuestring) { ota_url_ = …; nvs_set_str(nvs, "ota_url", …) }` on namespace `wifi`. The routes are always registered; `show_ota_config` is only returned by `/advanced/config` for the page to decide whether to show the field.
- **Corrected pitfall**: an empty JSON string is still a cJSON string, so submitting the field empty **writes `""`** to `wifi/ota_url`, and `ota.cc:49-53` then falls back to `CONFIG_OTA_URL` — no NVS erase needed. Whether the portal page omits the field when blank is UNVERIFIED (HTML not read). The pinned release is `~3.3.1` while only `main` was readable; re-check `managed_components/78__esp-wifi-connect/wifi_configuration_ap.cc` after `idf.py reconfigure`. Hard reset path remains `idf.py erase-flash`.
- Entering config mode: no stored SSID (`wifi_board.cc:100-114`), 60 s connect timeout (`:27,160-167`), BOOT click while `kDeviceStateStarting` (board `.cc:207-212`), MCP tool `self.system.reconfigure_wifi` (`.cc:304-310`). Hotspot provisioning is the Kconfig default (`Kconfig.projbuild:1007-1009`); the hint shows AP SSID + portal URL (`wifi_board.cc:178-183`).

## 8. AEC / AFE

- `USE_AUDIO_PROCESSOR` default y, `depends on (ESP32S3||P4||S31) && SPIRAM` (`Kconfig.projbuild:969-975`). `USE_DEVICE_AEC` default n, depends on `USE_AUDIO_PROCESSOR` and a board whitelist including `…AMOLED_1_75C` (`:976-985`). `USE_SERVER_AEC` is mutually exclusive (`application.cc:27-28` `#error`); compile-time `aec_mode_ = kAecOnDeviceSide` (`:29-30`).
- BOOT double-click toggles AEC when idle (board `.cc:216-223`); `SetAecMode` writes no Settings → **not persisted** (`application.cc:1333-1358`). Only yunliao-s3 persists AEC (`yunliao_s3.cc:113,180,235,251`).
- AFE config (`afe_audio_engine.cc:132-179`): input format `M…R` from codec channels; `AFE_TYPE_FD`, `AFE_MODE_LOW_COST`; `aec_init = input_reference`, `AEC_MODE_FD_LOW_COST`, `AEC_NLP_LEVEL_VERYAGGR`; `ns_init = false`; **`vad_init = kUseAfeForVoiceProcessing`, i.e. VAD on iff `CONFIG_USE_AUDIO_PROCESSOR=y`** (`:18-22,154`); `VAD_MODE_0`, `vad_min_noise_ms 100`; `agc_init = false`; `AFE_MEMORY_ALLOC_MORE_PSRAM`. NS intentionally off (`main/audio/README.md:22-24`).
- **Boot-time pipeline print is expected to show WakeNet and AEC off**: right after creation the engine calls `disable_wakenet()` and, when the codec has a reference channel, `disable_aec()`, then `print_pipeline` (`:198-204`). They are re-enabled at runtime by `EnableWakeWordDetection`/`EnableDeviceAec` (e.g. `application.cc:1022`); do not read the initial print as a misconfiguration.
- With AEC on, default listening mode is realtime/barge-in (`application.cc:1189-1191`). Optional `CONFIG_WAKE_WORD_DETECTION_IN_LISTENING=y` (default n, `Kconfig.projbuild:957-967`).

## 9. Display / emotion UI structure (for feat-ui-*)

- Class chain: `Display` (`main/display/display.h:43-59`: `SetStatus`, `ShowNotification`, `SetEmotion`, `SetChatMessage`, `ClearChatMessages`, `SetTheme`, `SetPowerSaveMode`, `SetupUI`) → `LvglDisplay` → `LcdDisplay` (`lcd_display.h:14-31`: `top_bar_`, `status_bar_`, `content_`, `container_`, `side_bar_`, `bottom_bar_`, `preview_image_`, `emoji_label_`, `emoji_image_`, `gif_controller_`, `emoji_box_`, `chat_message_label_`) → `SpiLcdDisplay` (`:60`) → board-local `CustomLcdDisplay` (board `.cc:81-123`: overrides `SetupUI()`, pads status bar 10 %, registers `rounder_event_cb` on `LV_EVENT_INVALIDATE_AREA` to even-align areas for CO5300).
- Face rendering: `LcdDisplay::SetEmotion(const char*)` (`lcd_display.cc:1132-…`) → `LvglTheme::emoji_collection()` image, else Noto emoji glyph (`font_noto_emoji_30_4`, `:26,32`), else Material Symbols. Default-style `SetupUI` starts at `lcd_display.cc:854` (WeChat block ends `:852`); `emoji_box_` centered (`:883-897`). `LvglTheme` holds `emoji_collection`, `background_image` (`lvgl_display/lvgl_theme.h:29-30,53-57`).
- Emotion sources: server `llm` JSON `emotion` (`application.cc:670-675`), `Alert()` (`:752-757`), state transitions set `"neutral"` (`:1016-1029`), boot `"robot_2"` (`:439`).
- `USE_EMOTE_MESSAGE_STYLE` is **not** selectable for this board (`Kconfig.projbuild:869-872` whitelist). feat-ui-face should subclass `SpiLcdDisplay`/`CustomLcdDisplay`, override `SetupUI()`/`SetEmotion()`, draw with LVGL 9.5 under `DisplayLockGuard`, keep the rounder. Backlight = panel command 0x51 via `CustomBacklight` (`.cc:125-142`).
- Power save: `PowerSaveTimer(cpu_max_freq, seconds_to_sleep, seconds_to_shutdown)` (`power_save_timer.h:10`); board uses `(-1, 60, 300)` (`.cc:155`): after 60 s idle → `OnEnterSleepMode` (display power-save + brightness 20), after 300 s → `OnShutdownRequest` → `pmic_->PowerOff()` (`.cc:156-163`; `power_save_timer.cc:62-104`); `cpu_max_freq == -1` skips the esp_pm/light-sleep branch (`:78-97`). Timer is enabled only while discharging (`.cc:355-360`) and is suppressed if NVS `wifi/sleep_mode` is false (`power_save_timer.cc:31-36`).

## 10. Acceptance checklist

- [ ] `idf.py --version` reports 6.1.x; tools live under `$IDF_TOOLS_PATH` in the project; no `~/.espressif` created; `esptool version` >= 5.
- [ ] `esptool flash-id` output recorded; `backup/flash_backup.bin` size equals detected flash; `backup/` git-ignored.
- [ ] `managed_components/` populated; `python scripts/build.py --list-wake-words | grep wn9_jarvis_tts` prints a row.
- [ ] Build log prints `reported_name: esp32-s3-touch-amoled-1.75c-weixue` (or the chosen name), `wake_word: wn9_jarvis_tts`, `language: en-US`, and the `CONFIG_OTA_URL` line; `grep -E 'CONFIG_SR_WN_WN9_JARVIS_TTS=y|CONFIG_USE_AFE_WAKE_WORD=y|CONFIG_LANGUAGE_EN_US=y|CONFIG_USE_DEVICE_AEC=y|CONFIG_USE_AUDIO_PROCESSOR=y|CONFIG_FLASH_DEFAULT_ASSETS=y' sdkconfig` → 6 hits; `grep CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y sdkconfig` → 0 hits.
- [ ] `idf.py flash` writes the `assets` partition; monitor shows exactly one `AfeAudioEngine: Model 0: wn9_jarvis_tts` line and no `Model 1:`.
- [ ] Server up first: `curl http://<mac-ip>:8003/xiaozhi/ota/` prints a LAN `ws://…:8000/xiaozhi/v1/` (not 127.0.0.1 or a container IP); `data/bin` is empty.
- [ ] Monitor: `Ota: Current version: 2.5.1`, OTA POST 200, `No mqtt section found !`, websocket section stored, no activation prompt; server log shows "未配置MQTT网关，为设备 <mac> 下发WebSocket配置".
- [ ] Server log on first wake shows the device text `Jarvis`; after adding it to `wakeup_words` (or `enable_greeting: false`) no "Jarvis" utterance reaches the LLM.
- [ ] Say "Jarvis" ×10 → ≥9 wakes; device opens WS and completes one turn with default providers.
- [ ] Speak over TTS: device-side AEC barge-in works (listening mode realtime).
- [ ] Portal Advanced tab shows the OTA URL field; changing it and rebooting changes the OTA target; submitting it empty makes the device fall back to `CONFIG_OTA_URL`.
- [ ] DESIGN.md feat-firmware-base row updated from 「你好小智」 to "Jarvis".

## UNVERIFIED
- esp-wifi-connect: the fetched source is GitHub `main`; the component pin is `~3.3.1` (idf_component.yml:27) and the tagged release was not fetched, so `main` == shipped version is UNVERIFIED. Re-check managed_components/78__esp-wifi-connect/wifi_configuration_ap.cc after `idf.py reconfigure`. Line numbers reported by the fetch (~730-735 for ota_url, ~185-186 SSID, ~206 nvs_open) differ from the skeptics' (605-608) — cite by code, not line.
- esp-wifi-connect portal HTML (assets/wifi_configuration.html or index.html): whether a blank OTA URL field is sent as "" or omitted is UNVERIFIED (not read).
- esp-wifi-connect SsidManager NVS namespace and key names for SSID/password are UNVERIFIED (component source not read for that part).
- esp-sr _MODEL_INFO_ for wn9_jarvis_tts and src/model_path.c get_wake_words_from_info were fetched from `master`, not from the 2.4.7 commit (2f8c4b04); identical content at the pinned version is UNVERIFIED by me (the skeptics report the same string at that commit). The `depends on IDF_TARGET_ESP32S3 || ESP32P4 || ESP32S31` line for SR_WN_WN9_JARVIS_TTS was reported by the skeptics at 2f8c4b04; the master fetch only returned the bool/default lines.
- Actual flash size of the physical 1.75C unit (Waveshare says 32 MB, upstream builds 16 MB) — confirm with `esptool flash-id` before applying the 32 MB override.
- 32m.csv auto-assigned offsets for nvsfactory/nvs/otadata/phy_init (0x9000/0x3B000/0x10D000/0x10F000) are derived arithmetic, not read from a generated table; confirm with `idf.py partition-table`.
- Installed esptool major version under the project venv (requirements pin is unversioned) — run `esptool version`.
