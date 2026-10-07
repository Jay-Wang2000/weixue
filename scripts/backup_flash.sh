#!/usr/bin/env bash
# Read-only full-flash backup under the device lock to ${WEIXUE_BACKUP_DIR:-$HOME/Projects/weixue/backup}/flash-<timestamp>.bin with the device MAC in flash-<timestamp>.mac and its sha256 in flash-<timestamp>.sha256, all mode 0600. Never erases or writes the device.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
backup_dir="${WEIXUE_BACKUP_DIR:-$HOME/Projects/weixue/backup}"
umask 077

# shellcheck source=scripts/device.sh
. "$root/scripts/device.sh"
device_select_port "${1:-}"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
out="$backup_dir/flash-$(date +%Y%m%d-%H%M%S).bin"
partial="$out.partial"
# shellcheck disable=SC2016
device_lock_cleanup='rm -f "$partial"'
device_lock_acquire backup_flash.sh

device_flash_id
mac_before="$mac"
"$esptool" --chip esp32s3 --port "$port" read-flash 0 "${FLASH_SIZE:-all}" "$partial"
device_flash_id
[ "$mac" = "$mac_before" ] || die "device MAC changed during the read ($mac_before, then $mac)"
echo "$mac" > "${out%.bin}.mac"
echo "$(shasum -a 256 "$partial" | awk '{print $1}')  $(basename "$out")" > "${out%.bin}.sha256"
mv "$partial" "$out"
chmod 600 "$out" "${out%.bin}.mac" "${out%.bin}.sha256"
backup_verify_digest "$out"
echo "mac: $mac"
echo "file: $out ($(wc -c < "$out" | tr -d ' ') bytes)"
