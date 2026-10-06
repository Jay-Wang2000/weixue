# Toolchain

Everything installs under `~/Projects/weixue-vendor` (override with `WEIXUE_VENDOR`) or the project `.venv`; nothing global, no shell-rc edits. Pins are in `vendor.lock`.

## Fresh-Mac setup

Prerequisites: git, Xcode command-line tools, python3 >= 3.10 first on PATH (Homebrew 3.14 tested; Apple `/usr/bin/python3` is too old).

```bash
# 1. Vendor upstream firmware at the pinned commit
mkdir -p ~/Projects/weixue-vendor && cd ~/Projects/weixue-vendor
git clone https://github.com/78/xiaozhi-esp32
git -C xiaozhi-esp32 checkout 0d576d3d4c049c6f55eaf879725dc23e516511b4

# 2. ESP-IDF v6.1, tools (incl. pinned cmake and ninja) under the vendor dir
git clone --depth 1 --branch v6.1 --recursive --shallow-submodules https://github.com/espressif/esp-idf.git
export IDF_TOOLS_PATH=~/Projects/weixue-vendor/.espressif IDF_PATH=~/Projects/weixue-vendor/esp-idf
(cd esp-idf && ./install.sh esp32s3)
python3 "$IDF_PATH/tools/idf_tools.py" install cmake ninja

# 3. Project venv with pinned esptool
cd ~/Projects/weixue
python3 -m venv .venv && .venv/bin/pip install esptool==5.4.0

# 4. Verify
source scripts/env.sh
idf.py --version            # ESP-IDF v6.1
```

## Use

```bash
source scripts/env.sh       # idf.py, cmake, ninja on PATH; IDF_PATH, XIAOZHI_PATH set
```

## Flash backup (read-only)

```bash
ls /dev/cu.usbmodem*        # expect /dev/cu.usbmodem2101
scripts/backup_flash.sh [port]
```

Reads the full flash (size auto-detected; override with `FLASH_SIZE`) to `${WEIXUE_BACKUP_DIR:-$HOME/Projects/weixue/backup}/flash-<timestamp>.bin` and prints its byte count and sha256. `backup/` is gitignored.

Restore (manual, destructive): `.venv/bin/esptool --chip esp32s3 --port <port> write-flash 0 <backup-file>`.

## Board target check

```bash
source scripts/env.sh
rm -rf /tmp/xz-scratch && cp -R "$XIAOZHI_PATH" /tmp/xz-scratch && cd /tmp/xz-scratch
grep -n "config BOARD_TYPE_WAVESHARE_ESP32_S3_TOUCH_AMOLED_1_75C" main/Kconfig.projbuild
idf.py set-target esp32s3
```

The 1.75C board lives in `main/boards/waveshare/esp32-s3-touch-amoled-1.75`.
