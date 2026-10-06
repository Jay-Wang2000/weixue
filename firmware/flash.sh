#!/usr/bin/env bash
# Flashes the image recorded by firmware/build.sh (bootloader, partition table, otadata, app, assets) with the project .venv esptool after verifying the build manifest and a sha256-checked full backup of the connected device. NVS is not erased.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
vendor="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}"
work="${WEIXUE_FW_WORK:-$vendor/fw-work/$(basename "$root")}"
manifest=weixue-build.sha256
backup_dir="${WEIXUE_BACKUP_DIR:-$HOME/Projects/weixue/backup}"
flash_bytes=33554432
board_dir=waveshare/esp32-s3-touch-amoled-1.75

die() { echo "error: $*" >&2; exit 1; }

if [ -n "${1:-}" ]; then
  port="$1"
else
  shopt -s nullglob
  ports=(/dev/cu.usbmodem*)
  [ "${#ports[@]}" -eq 1 ] || die "expected one /dev/cu.usbmodem* port, found: ${ports[*]:-none}; pass one as \$1"
  port="${ports[0]}"
fi
echo "port: $port"
[ -f "$work/$manifest" ] || die "no $manifest in $work; run firmware/build.sh"
(cd "$work" && shasum -a 256 -c --quiet "$manifest") \
  || die "$work differs from the image firmware/build.sh verified; run firmware/build.sh"
for f in sdkconfig build/flash_args build/xiaozhi.bin; do
  grep -q "  $f\$" "$work/$manifest" || die "$manifest does not cover $f; run firmware/build.sh"
done
cmp -s "$work/main/boards/$board_dir/config.weixue.json" "$root/firmware/board/config.weixue.json" \
  || die "$work was built from a different config.weixue.json; run firmware/build.sh"
ota_want="$(python3 -c 'import json,sys
for b in json.load(open(sys.argv[1]))["builds"]:
    for l in b["sdkconfig_append"]:
        if l.startswith("CONFIG_OTA_URL="): print(l)' "$root/firmware/board/config.weixue.json")"
grep -qxF "$ota_want" "$work/sdkconfig" || die "$work/sdkconfig CONFIG_OTA_URL differs from config.weixue.json; run firmware/build.sh"

# shellcheck source=scripts/venv.sh
. "$root/scripts/venv.sh"
id_out="$("$esptool" --chip esp32s3 --port "$port" flash-id)"
echo "$id_out" | grep -E 'Detected flash size|Chip|MAC'
echo "$id_out" | grep -q 'Detected flash size: 32MB' || die "flash is not 32MB; the 32MB partition table does not apply"
mac="$(echo "$id_out" | awk '$1=="MAC:"{print $NF; exit}')"
[ -n "$mac" ] || die "no MAC in esptool flash-id output"

shopt -s nullglob
matched=() untagged=()
for f in "$backup_dir"/flash-*.bin; do
  [ "$(wc -c < "$f" | tr -d ' ')" -eq "$flash_bytes" ] || continue
  if [ -f "${f%.bin}.mac" ]; then
    if [ "$(cat "${f%.bin}.mac")" = "$mac" ]; then matched+=("$f"); fi
  else
    untagged+=("$f")
  fi
done
if [ "${#matched[@]}" -gt 0 ]; then
  backup="${matched[${#matched[@]}-1]}"
elif [ "${#untagged[@]}" -gt 0 ]; then
  backup="${untagged[${#untagged[@]}-1]}"
  echo "warning: no backup tagged with MAC $mac; using untagged $backup" >&2
else
  die "no full $flash_bytes-byte backup of $mac in $backup_dir; run scripts/backup_flash.sh"
fi
echo "backup: $backup"
[ -f "${backup%.bin}.sha256" ] || die "$backup has no $(basename "${backup%.bin}").sha256 beside it; run scripts/backup_flash.sh"
(cd "$backup_dir" && shasum -a 256 -c "$(basename "${backup%.bin}.sha256")") || die "$backup fails its sha256 check"

cd "$work/build"
"$esptool" --chip esp32s3 --port "$port" --baud 460800 --before default-reset --after hard-reset \
  write-flash @flash_args
