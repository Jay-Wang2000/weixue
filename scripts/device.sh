# Source with $root set to the repo root and `set -euo pipefail`: serial port, device MAC, backup sidecar checks and the device lock for flash.sh, backup_flash.sh and adopt_backup.sh.
# shellcheck source=scripts/venv.sh
. "$root/scripts/venv.sh"
# shellcheck source=scripts/device_lock.sh
. "$root/scripts/device_lock.sh"
flash_bytes=33554432

die() { echo "error: $*" >&2; exit 1; }

# Sets $port from $1 or the single /dev/cu.usbmodem* device.
device_select_port() {
  if [ -n "${1:-}" ]; then
    port="$1"
  else
    local ports
    shopt -s nullglob
    ports=(/dev/cu.usbmodem*)
    shopt -u nullglob
    [ "${#ports[@]}" -eq 1 ] || die "expected one /dev/cu.usbmodem* port, found: ${ports[*]:-none}; pass one as \$1"
    port="${ports[0]}"
  fi
  [ -e "$port" ] || die "$port does not exist"
  echo "port: $port"
}

# Sets $mac and $flash_size_line from esptool flash-id on $port.
device_flash_id() {
  local id_out
  id_out="$("$esptool" --chip esp32s3 --port "$port" flash-id)"
  echo "$id_out" | grep -E 'Detected flash size|Chip|MAC' >&2
  mac="$(echo "$id_out" | awk '$1=="MAC:"{print $NF; exit}')"
  [ -n "$mac" ] || die "no MAC in esptool flash-id output"
  flash_size_line="$(echo "$id_out" | grep 'Detected flash size' || true)"
}

# Checks that <bin>'s .sha256 sidecar is one line naming <bin> and that the file's sha256 equals it.
backup_verify_digest() {
  local bin="$1" sidecar="${1%.bin}.sha256" want name extra got
  [ -f "$sidecar" ] || die "$bin has no $(basename "$sidecar")"
  [ "$(wc -l < "$sidecar" | tr -d ' ')" = 1 ] || die "$sidecar is not a single sha256 line"
  read -r want name extra < "$sidecar"
  [ "$name" = "$(basename "$bin")" ] && [ -z "$extra" ] || die "$sidecar names '$name', not $(basename "$bin")"
  got="$(shasum -a 256 "$bin" | awk '{print $1}')"
  [ "$got" = "$want" ] || die "$bin sha256 $got differs from $sidecar ($want)"
  echo "backup sha256: $got"
}
