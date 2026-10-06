#!/usr/bin/env bash
# Read-only full-flash backup to ${WEIXUE_BACKUP_DIR:-$HOME/Projects/weixue/backup}/flash-<timestamp>.bin. Never erases or writes.
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

# shellcheck disable=SC1091
. "$root/.venv/bin/activate"
mkdir -p "$backup_dir"
out="$backup_dir/flash-$(date +%Y%m%d-%H%M%S).bin"
esptool --chip esp32s3 --port "$port" flash-id
esptool --chip esp32s3 --port "$port" read-flash 0 "${FLASH_SIZE:-all}" "$out"
echo "file: $out ($(wc -c < "$out" | tr -d ' ') bytes)"
shasum -a 256 "$out"
