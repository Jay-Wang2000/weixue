#!/usr/bin/env bash
# Builds the weixue firmware in a scratch copy of the vendored xiaozhi-esp32 clone, writes the sha256 of sdkconfig, config.weixue.json and every flashed file to $work/weixue-build.sha256, and prints build time and binary sizes.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
vendor="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}"
src="$vendor/xiaozhi-esp32"
work="${WEIXUE_FW_WORK:-$vendor/fw-work/$(basename "$root")}"
manifest=weixue-build.sha256
board_dir=waveshare/esp32-s3-touch-amoled-1.75
build_name=esp32-s3-touch-amoled-1.75c-weixue
language=en-US
wake_word=wn9_jarvis_tts

die() { echo "error: $*" >&2; exit 1; }

expected="$(awk '$1=="xiaozhi-esp32"{print $3}' "$root/vendor.lock")"
[ "$(git -C "$src" rev-parse HEAD)" = "$expected" ] || die "$src is not at vendor.lock commit $expected"
[ -z "$(git -C "$src" status --porcelain)" ] || die "$src has local changes"

[ "$(cd "$src" && pwd -P)" != "$(mkdir -p "$work" && cd "$work" && pwd -P)" ] || die "WEIXUE_FW_WORK must not be the vendored clone"
[ -z "$(ls -A "$work")" ] || [ -f "$work/main/boards/$board_dir/config.weixue.json" ] \
  || die "$work is not empty and not a weixue scratch copy"
rm -f "$work/$manifest"
rsync -a --delete \
  --exclude /.git --exclude /managed_components --exclude /build \
  --exclude /sdkconfig --exclude /sdkconfig.old --exclude /dependencies.lock \
  --exclude /main/assets/lang_config.h \
  "$src/" "$work/"
cp "$root/firmware/board/config.weixue.json" "$work/main/boards/$board_dir/config.weixue.json"
[ -f "$root/firmware/dependencies.lock" ] && cp "$root/firmware/dependencies.lock" "$work/dependencies.lock"
shopt -s nullglob
for patch in "$root"/firmware/patches/*.patch; do
  git -C "$work" apply --check "$patch" || die "$(basename "$patch") does not apply to vendor.lock commit $expected"
  git -C "$work" apply "$patch"
  echo "patch: $(basename "$patch")"
done
shopt -u nullglob

# shellcheck disable=SC1091
. "$root/scripts/env.sh" >/dev/null
cd "$work"
start=$(date +%s)
python scripts/build.py "$board_dir" -c config.weixue.json --name "$build_name" \
  --language "$language" --wake-word "$wake_word"
elapsed=$(( $(date +%s) - start ))

for want in CONFIG_SR_WN_WN9_JARVIS_TTS=y CONFIG_USE_AFE_WAKE_WORD=y CONFIG_LANGUAGE_EN_US=y \
            CONFIG_USE_DEVICE_AEC=y CONFIG_USE_AUDIO_PROCESSOR=y CONFIG_FLASH_DEFAULT_ASSETS=y \
            CONFIG_RECEIVE_CUSTOM_MESSAGE=y CONFIG_ESPTOOLPY_FLASHSIZE_32MB=y \
            'CONFIG_PARTITION_TABLE_CUSTOM_FILENAME="partitions/v2/32m.csv"'; do
  grep -qxF "$want" sdkconfig || die "sdkconfig lacks $want"
done
wn="$(grep -E '^CONFIG_SR_WN_.*=y$' sdkconfig || true)"
[ "$wn" = "CONFIG_SR_WN_WN9_JARVIS_TTS=y" ] || die "unexpected wake word models: $wn"
ota_want="$(python3 -c 'import json,sys
for b in json.load(open(sys.argv[1]))["builds"]:
    for l in b["sdkconfig_append"]:
        if l.startswith("CONFIG_OTA_URL="): print(l)' "$root/firmware/board/config.weixue.json")"
[ -n "$ota_want" ] || die "config.weixue.json has no CONFIG_OTA_URL"
grep -qxF "$ota_want" sdkconfig || die "sdkconfig CONFIG_OTA_URL differs from config.weixue.json ($ota_want)"
echo "$ota_want"
power_off="$(grep -E '^CONFIG_WEIXUE_BATTERY_IDLE_POWER_OFF_SECONDS=[0-9]+$' sdkconfig || true)"
[ -n "$power_off" ] || die "sdkconfig lacks CONFIG_WEIXUE_BATTERY_IDLE_POWER_OFF_SECONDS (power patch not applied)"
echo "$power_off"

if [ -f "$root/firmware/dependencies.lock" ] && ! cmp -s dependencies.lock "$root/firmware/dependencies.lock"; then
  echo "warning: $work/dependencies.lock differs from firmware/dependencies.lock" >&2
fi

head -1 build/flash_args | grep -q -- '--flash-size 32MB' || die "build/flash_args is not a 32MB layout"
{ echo sdkconfig; echo "main/boards/$board_dir/config.weixue.json"; echo build/flash_args
  awk 'NR>1{print "build/"$2}' build/flash_args; } | while read -r f; do shasum -a 256 "$f"; done > "$manifest"
echo "build_seconds: $elapsed"
for f in build/bootloader/bootloader.bin build/partition_table/partition-table.bin \
         build/xiaozhi.bin build/generated_assets.bin build/merged-binary.bin; do
  [ -f "$f" ] && echo "size: $f $(wc -c < "$f" | tr -d ' ')"
done
cat "$manifest"
echo "work_dir: $work"
