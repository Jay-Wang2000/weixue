#!/usr/bin/env bash
# Tags an untagged full backup with the connected device's MAC after checking, under the device lock, that the board:uuid in the device's NVS equals the one in the backup; writes the .mac (and a .sha256 when none exists) and sets the backup and sidecars to mode 0600.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
umask 077
nvs_offset=0x3B000
nvs_size=0xD2000

[ -n "${1:-}" ] || { echo "usage: $0 <backup.bin> [port]" >&2; exit 2; }
backup="$1"
# shellcheck source=scripts/device.sh
. "$root/scripts/device.sh"
[ -f "$backup" ] || die "$backup does not exist"
[ "$(wc -c < "$backup" | tr -d ' ')" -eq "$flash_bytes" ] || die "$backup is not a full $flash_bytes-byte backup"
[ ! -e "${backup%.bin}.mac" ] || die "$backup is already tagged ($(cat "${backup%.bin}.mac"))"
if [ -f "${backup%.bin}.sha256" ]; then backup_verify_digest "$backup"; fi
want="$(python3 "$root/scripts/nvs_board_uuid.py" "$backup")" || die "$backup has no NVS board:uuid"
[ "$(echo "$want" | wc -l | tr -d ' ')" = 1 ] || die "$backup has more than one NVS board:uuid"

device_select_port "${2:-}"
tmp="$(mktemp -d)"
# shellcheck disable=SC2016
device_lock_cleanup='rm -rf "$tmp"'
device_lock_acquire adopt_backup.sh
device_flash_id
"$esptool" --chip esp32s3 --port "$port" read-flash "$nvs_offset" "$nvs_size" "$tmp/nvs.bin" >/dev/null
got="$(python3 "$root/scripts/nvs_board_uuid.py" --offset 0 "$tmp/nvs.bin")" || die "the device NVS has no board:uuid"
[ "$got" = "$want" ] || die "$backup is not from the device at $port (MAC $mac): NVS board:uuid differs"
echo "board:uuid sha256 prefix: ${got:0:16} (backup and device)"

echo "$mac" > "${backup%.bin}.mac"
[ -f "${backup%.bin}.sha256" ] \
  || echo "$(shasum -a 256 "$backup" | awk '{print $1}')  $(basename "$backup")" > "${backup%.bin}.sha256"
chmod 600 "$backup" "${backup%.bin}.mac" "${backup%.bin}.sha256"
backup_verify_digest "$backup"
echo "tagged: $backup mac $mac"
