#!/usr/bin/env bash
# Flashes the image recorded by firmware/build.sh (bootloader, partition table, otadata, app, assets) with the project .venv esptool, under the device lock, after checking the build manifest, that the build's provenance equals the current checkout's, and that the newest full backup tagged with the connected device's MAC matches its sha256. NVS is not erased.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
vendor="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}"
work="${WEIXUE_FW_WORK:-$vendor/fw-work/$(basename "$root")}"
manifest=weixue-build.sha256
provenance=weixue-build.provenance
backup_dir="${WEIXUE_BACKUP_DIR:-$HOME/Projects/weixue/backup}"
board_dir=waveshare/esp32-s3-touch-amoled-1.75

# shellcheck source=scripts/device.sh
. "$root/scripts/device.sh"
device_select_port "${1:-}"
[ -f "$work/$manifest" ] || die "no $manifest in $work; run firmware/build.sh"
(cd "$work" && shasum -a 256 -c --quiet "$manifest") \
  || die "$work differs from the image firmware/build.sh verified; run firmware/build.sh"
for f in sdkconfig build/flash_args build/xiaozhi.bin "$provenance"; do
  grep -q "  $f\$" "$work/$manifest" || die "$manifest does not cover $f; run firmware/build.sh"
done
current="$("$root/firmware/provenance.sh")"
if [ "$current" != "$(cat "$work/$provenance")" ]; then
  diff <(cat "$work/$provenance") <(echo "$current") | sed 's/^</built:  /; s/^>/current:/' | grep -E '^(built|current)' >&2 || true
  die "$work was built from different sources than $root; run firmware/build.sh"
fi
echo "provenance: matches the current checkout"
cat "$work/main/boards/$board_dir/face_diag.h"

device_lock_acquire flash.sh
device_flash_id
echo "$flash_size_line" | grep -q 'Detected flash size: 32MB' || die "flash is not 32MB; the 32MB partition table does not apply"

backup=""
shopt -s nullglob
for f in "$backup_dir"/flash-*.bin; do
  [ -f "${f%.bin}.mac" ] && [ "$(cat "${f%.bin}.mac")" = "$mac" ] || continue
  if [ "$(wc -c < "$f" | tr -d ' ')" -eq "$flash_bytes" ]; then backup="$f"; else echo "skipped (not $flash_bytes bytes): $f"; fi
done
shopt -u nullglob
[ -n "$backup" ] || die "no full $flash_bytes-byte backup tagged with MAC $mac in $backup_dir; run scripts/backup_flash.sh (or scripts/adopt_backup.sh for an untagged backup of this device)"
echo "backup: $backup"
backup_verify_digest "$backup"

cd "$work/build"
"$esptool" --chip esp32s3 --port "$port" --baud 460800 --before default-reset --after hard-reset \
  write-flash @flash_args
