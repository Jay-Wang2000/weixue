#!/usr/bin/env bash
# Read-only full-flash backup to ${WEIXUE_BACKUP_DIR:-$HOME/Projects/weixue/backup}/flash-<timestamp>.bin plus the device MAC in flash-<timestamp>.mac and its sha256 in flash-<timestamp>.sha256. Never erases or writes the device.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
backup_dir="${WEIXUE_BACKUP_DIR:-$HOME/Projects/weixue/backup}"

if [ -n "${1:-}" ]; then
  port="$1"
else
  ports=(/dev/cu.usbmodem*)
  [ -e "${ports[0]}" ] || { echo "no /dev/cu.usbmodem* port found" >&2; exit 1; }
  [ "${#ports[@]}" -eq 1 ] || { echo "multiple ports: ${ports[*]}; pass one as \$1" >&2; exit 1; }
  port="${ports[0]}"
fi
echo "port: $port"

# shellcheck source=scripts/venv.sh
. "$root/scripts/venv.sh"
mkdir -p "$backup_dir"
out="$backup_dir/flash-$(date +%Y%m%d-%H%M%S).bin"
id_out="$("$esptool" --chip esp32s3 --port "$port" flash-id)"
echo "$id_out"
mac="$(echo "$id_out" | awk '$1=="MAC:"{print $NF; exit}')"
[ -n "$mac" ] || { echo "no MAC in esptool flash-id output" >&2; exit 1; }
"$esptool" --chip esp32s3 --port "$port" read-flash 0 "${FLASH_SIZE:-all}" "$out"
echo "$mac" > "${out%.bin}.mac"
(cd "$backup_dir" && shasum -a 256 "$(basename "$out")") > "${out%.bin}.sha256"
echo "file: $out ($(wc -c < "$out" | tr -d ' ') bytes)"
cat "${out%.bin}.sha256"
