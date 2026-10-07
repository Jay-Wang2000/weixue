#!/usr/bin/env bash
# Tests flash.sh, backup_flash.sh, adopt_backup.sh, provenance.sh and nvs_board_uuid.py on the host: a copy of this checkout's firmware/, scripts/ and vendor.lock, a fake esptool and device image, a fake vendor tree and a temporary lock. Needs the vendored ESP-IDF (NVS parser and generator).
set -uo pipefail
src="$(cd "$(dirname "$0")/../.." && pwd)"
export IDF_PATH="${IDF_PATH:-$HOME/Projects/weixue-vendor/esp-idf}"
idf_py="$(ls "${IDF_TOOLS_PATH:-$HOME/Projects/weixue-vendor/.espressif}"/python_env/*/bin/python 2>/dev/null | head -1)"
[ -x "$idf_py" ] || { echo "error: no ESP-IDF python env" >&2; exit 1; }
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fails=0 out="" rc=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; echo "$out" | sed 's/^/     | /'; fails=$((fails + 1)); fi; }
run() { out="$("$@" 2>&1)"; rc=$?; }
g() { git -c user.name=test -c user.email=test@example.invalid "$@"; }
mode() { python3 -c 'import os, sys; print(oct(os.stat(sys.argv[1]).st_mode & 0o777)[2:])' "$1"; }
digest() { echo "$(shasum -a 256 "$1" | awk '{print $1}')  $(basename "$1")" > "${1%.bin}.sha256"; }
flip() { python3 -c 'import sys; f = open(sys.argv[1], "r+b"); f.seek(0x100000); b = f.read(1); f.seek(0x100000); f.write(bytes([b[0] ^ 1]))' "$1"; }

root="$tmp/repo" vendor="$tmp/vendor" work="$tmp/work" port="$tmp/port"
mkdir -p "$root" "$vendor" "$work"; : > "$port"
(cd "$src" && git ls-files -co --exclude-standard -- firmware scripts vendor.lock) | rsync -a --files-from=- "$src/" "$root/"
g -C "$root" init -q && g -C "$root" add -A && g -C "$root" commit -qm base
for r in xiaozhi-esp32 esp-idf; do g init -q "$vendor/$r" && g -C "$vendor/$r" commit -q --allow-empty -m base; done

mkdir -p "$root/.venv/bin"
printf '#!/bin/sh\nawk '\''$1=="esptool"{print $3}'\'' "%s/vendor.lock"\n' "$root" > "$root/.venv/bin/python"
cat > "$root/.venv/bin/esptool" <<'PY'
#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
i = next(i for i, a in enumerate(args) if a in ("flash-id", "read-flash", "write-flash"))
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(args) + "\n")
if args[i] == "flash-id":
    print("Chip type:          ESP32-S3")
    print("MAC:                " + open(os.environ["FAKE_MAC_FILE"]).read().strip())
    print("Detected flash size: 32MB")
elif args[i] == "read-flash":
    off, size, dest = args[i + 1:i + 4]
    data = open(os.environ["FAKE_DEVICE"], "rb").read()[int(off, 0):]
    open(dest, "wb").write(data if size == "all" else data[:int(size, 0)])
    if os.environ.get("FAKE_MAC_AFTER_READ"):
        open(os.environ["FAKE_MAC_FILE"], "w").write(os.environ["FAKE_MAC_AFTER_READ"])
PY
chmod +x "$root/.venv/bin/python" "$root/.venv/bin/esptool"

mac=aa:bb:cc:00:00:01 other=aa:bb:cc:00:00:02
export WEIXUE_VENDOR="$vendor" WEIXUE_FW_WORK="$work" WEIXUE_BACKUP_DIR="$tmp/backup"
export WEIXUE_DEVICE_LOCK="$tmp/lock" WEIXUE_DEVICE_LOCK_POLL=1 WEIXUE_DEVICE_LOCK_WAIT=2
export FAKE_DEVICE="$tmp/device.bin" FAKE_MAC_FILE="$tmp/mac" FAKE_LOG="$tmp/esptool.log"
unset WEIXUE_DEVICE_LOCK_OWNER FAKE_MAC_AFTER_READ
echo "$mac" > "$FAKE_MAC_FILE"

nvs() { printf 'key,type,encoding,value\n%s,namespace,,\n%s,data,string,%s\n' "$1" "$2" "$3" > "$tmp/nvs.csv"
        "$idf_py" -m esp_idf_nvs_partition_gen generate "$tmp/nvs.csv" "$4" 0xD2000 >/dev/null; }
image() { python3 -c 'import sys
with open(sys.argv[1], "wb") as f:
    f.truncate(32 << 20); f.seek(0x3B000); f.write(open(sys.argv[2], "rb").read())' "$1" "$2"; }
nvs board uuid uuid-device-1 "$tmp/nvs-device.bin"
nvs board uuid uuid-other-2 "$tmp/nvs-other.bin"
nvs wifi ssid test-ssid "$tmp/nvs-none.bin"
image "$FAKE_DEVICE" "$tmp/nvs-device.bin"
image "$tmp/other.bin" "$tmp/nvs-other.bin"
image "$tmp/none.bin" "$tmp/nvs-none.bin"

# nvs_board_uuid.py
uuid_hash="$(printf %s uuid-device-1 | shasum -a 256 | awk '{print $1}')"
run python3 "$root/scripts/nvs_board_uuid.py" "$FAKE_DEVICE"
check "nvs_board_uuid: full image" '[ $rc -eq 0 ] && [ "$out" = "$uuid_hash" ]'
run python3 "$root/scripts/nvs_board_uuid.py" --offset 0 "$tmp/nvs-device.bin"
check "nvs_board_uuid: partition dump" '[ $rc -eq 0 ] && [ "$out" = "$uuid_hash" ]'
run python3 "$root/scripts/nvs_board_uuid.py" "$tmp/other.bin"
check "nvs_board_uuid: other uuid differs" '[ $rc -eq 0 ] && [ -n "$out" ] && [ "$out" != "$uuid_hash" ]'
run python3 "$root/scripts/nvs_board_uuid.py" "$tmp/none.bin"
check "nvs_board_uuid: no board:uuid exits 2" '[ $rc -eq 2 ] && [ -z "$out" ]'

# provenance.sh
prov() { "$root/firmware/provenance.sh"; }
p0="$(prov)"
check "provenance: repeatable" '[ "$(prov)" = "$p0" ] && echo "$p0" | grep -q "^uncommitted none$"'
patch="$(ls "$root"/firmware/patches/*.patch | head -1)"
echo "# test" >> "$patch"
p1="$(prov)"
check "provenance: patch edit changes its patch line" '[ "$(echo "$p1" | grep "^patch .* $(basename "$patch")$")" != "$(echo "$p0" | grep "^patch .* $(basename "$patch")$")" ] && ! echo "$p1" | grep -q "^uncommitted none$"'
g -C "$root" checkout -q -- firmware
echo "x" >> "$root/firmware/dependencies.lock"
check "provenance: dependencies.lock edit" '[ "$(prov | grep ^dependencies.lock)" != "$(echo "$p0" | grep ^dependencies.lock)" ]'
g -C "$root" checkout -q -- firmware
mkdir -p "$root/firmware/overrides/main"; echo x > "$root/firmware/overrides/main/test_new.txt"
check "provenance: new override listed" 'prov | grep -q "^override .* main/test_new.txt$"'
rm "$root/firmware/overrides/main/test_new.txt"
echo "# test" >> "$root/scripts/device.sh"; g -C "$root" commit -qam scripts
p2="$(prov)"
check "provenance: committed scripts change changes inputs" '[ "$(echo "$p2" | grep ^inputs)" != "$(echo "$p0" | grep ^inputs)" ] && echo "$p2" | grep -q "^uncommitted none$"'
g -C "$root" reset -q --hard HEAD~1
mkdir -p "$root/docs"; echo x > "$root/docs/test.md"; g -C "$root" add docs; g -C "$root" commit -qm docs
check "provenance: commit outside inputs keeps it" '[ "$(prov)" = "$p0" ]'
g -C "$vendor/esp-idf" commit -q --allow-empty -m next
check "provenance: ESP-IDF commit" '[ "$(prov | grep ^esp-idf)" != "$(echo "$p0" | grep ^esp-idf)" ]'
g -C "$vendor/esp-idf" reset -q --hard HEAD~1
check "provenance: restored" '[ "$(prov)" = "$p0" ]'

# flash.sh
fake_build() {
  rm -rf "$work"; mkdir -p "$work/build" "$work/main/boards/waveshare/esp32-s3-touch-amoled-1.75"
  echo "#define WEIXUE_FACE_DIAG 0" > "$work/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_diag.h"
  echo "CONFIG_TEST=y" > "$work/sdkconfig"
  printf -- '--flash-mode dio --flash-size 32MB\n0x200000 xiaozhi.bin\n' > "$work/build/flash_args"
  echo app > "$work/build/xiaozhi.bin"
  prov > "$work/weixue-build.provenance"
  (cd "$work" && shasum -a 256 weixue-build.provenance sdkconfig build/flash_args build/xiaozhi.bin > weixue-build.sha256)
}
flash() { : > "$FAKE_LOG"; run "$root/firmware/flash.sh" "$port"; }
wrote() { grep -q "write-flash @flash_args" "$FAKE_LOG"; }
nolock() { [ ! -e "$WEIXUE_DEVICE_LOCK" ]; }
fake_build
b="$WEIXUE_BACKUP_DIR"; mkdir -p "$b"
b1="$b/flash-20260101-000001.bin" b2="$b/flash-20260101-000002.bin" b3="$b/flash-20260101-000003.bin"

flash
check "flash: no backup refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "no full 33554432-byte backup tagged with MAC $mac" && nolock'
cp "$FAKE_DEVICE" "$b1"; digest "$b1"
flash
check "flash: untagged backup refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "no full .* tagged with MAC $mac" && nolock'
echo "$other" > "${b1%.bin}.mac"
flash
check "flash: backup tagged with another MAC refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "tagged with MAC $mac" && nolock'
echo "$mac" > "${b1%.bin}.mac"
flash
check "flash: same backup tagged with the device MAC flashes" '[ $rc -eq 0 ] && wrote && echo "$out" | grep -q "^backup: $b1$" && nolock'
head -c 1048576 "$FAKE_DEVICE" > "$b2"; echo "$mac" > "${b2%.bin}.mac"; digest "$b2"
flash
check "flash: newer short tagged backup skipped, older full one used" '[ $rc -eq 0 ] && wrote && echo "$out" | grep -q "skipped (not 33554432 bytes): $b2" && echo "$out" | grep -q "^backup: $b1$"'
echo "$other" > "${b1%.bin}.mac"
flash
check "flash: only a short tagged backup refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "no full 33554432-byte backup tagged"'
echo "$mac" > "${b1%.bin}.mac"; rm -f "$b2" "${b2%.bin}".*
cp "$b1" "$b3"; echo "$mac" > "${b3%.bin}.mac"; digest "$b3"; flip "$b3"
flash
check "flash: digest mismatch refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "$b3 sha256 .* differs" && nolock'
cp "$b1" "$b3"; rm "${b3%.bin}.sha256"
flash
check "flash: missing .sha256 refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "has no flash-20260101-000003.sha256"'
cp "${b1%.bin}.sha256" "${b3%.bin}.sha256"
flash
check "flash: .sha256 naming another file refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "names .flash-20260101-000001.bin."'
rm -f "$b3" "${b3%.bin}".*

echo "# test" >> "$patch"
flash
check "flash: patch edited after build refused before serial access" '[ $rc -ne 0 ] && ! grep -q . "$FAKE_LOG" && echo "$out" | grep -q "built from different sources"'
g -C "$root" checkout -q -- firmware
echo "x" >> "$root/firmware/dependencies.lock"
flash
check "flash: dependencies.lock edited after build refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "built from different sources"'
g -C "$root" checkout -q -- firmware
echo "# test" >> "$root/scripts/env.sh"
flash
check "flash: scripts edited after build refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "built from different sources"'
g -C "$root" checkout -q -- scripts
sed -i.orig 's/^esp-idf .*/esp-idf 0000000000000000000000000000000000000000/' "$work/weixue-build.provenance"
(cd "$work" && shasum -a 256 weixue-build.provenance sdkconfig build/flash_args build/xiaozhi.bin > weixue-build.sha256)
flash
check "flash: other ESP-IDF in provenance refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "^built: *esp-idf 0000" && echo "$out" | grep -q "built from different sources"'
fake_build
echo x >> "$work/build/xiaozhi.bin"
flash
check "flash: image changed after build refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "differs from the image"'
fake_build
(cd "$work" && shasum -a 256 sdkconfig build/flash_args build/xiaozhi.bin > weixue-build.sha256)
flash
check "flash: manifest without provenance refused" '[ $rc -ne 0 ] && ! wrote && echo "$out" | grep -q "does not cover weixue-build.provenance"'
fake_build
flash
check "flash: rebuilt checkout flashes" '[ $rc -eq 0 ] && wrote && nolock'

live="$(sh -c 'sleep 9101 >/dev/null 2>&1 & echo $!')"
mkdir "$WEIXUE_DEVICE_LOCK"; echo "pid=$live since=x by=other" > "$WEIXUE_DEVICE_LOCK/owner"
flash
check "flash: live lock of another owner refused, lock kept" '[ $rc -ne 0 ] && ! grep -q . "$FAKE_LOG" && echo "$out" | grep -q "still held" && grep -q "pid=$live " "$WEIXUE_DEVICE_LOCK/owner"'
: > "$FAKE_LOG"; out="$(WEIXUE_DEVICE_LOCK_OWNER="$live" "$root/firmware/flash.sh" "$port" 2>&1)"; rc=$?
check "flash: lock held by caller used and kept" '[ $rc -eq 0 ] && wrote && echo "$out" | grep -q "held by caller" && grep -q "pid=$live " "$WEIXUE_DEVICE_LOCK/owner"'
kill "$live"; rm -rf "$WEIXUE_DEVICE_LOCK"

# backup_flash.sh
bk="$tmp/bk"
: > "$FAKE_LOG"; out="$(WEIXUE_BACKUP_DIR="$bk" "$root/scripts/backup_flash.sh" "$port" 2>&1)"; rc=$?
nb="$(ls "$bk"/flash-*.bin 2>/dev/null)"
check "backup: writes bin, mac and sha256" '[ $rc -eq 0 ] && [ -n "$nb" ] && cmp -s "$nb" "$FAKE_DEVICE" && [ "$(cat "${nb%.bin}.mac")" = "$mac" ] && (cd "$bk" && shasum -a 256 -c --quiet "$(basename "${nb%.bin}.sha256")") && nolock'
check "backup: files 0600, directory 0700, no partial" '[ "$(mode "$nb")" = 600 ] && [ "$(mode "${nb%.bin}.mac")" = 600 ] && [ "$(mode "${nb%.bin}.sha256")" = 600 ] && [ "$(mode "$bk")" = 700 ] && [ -z "$(ls "$bk" | grep partial)" ]'
: > "$FAKE_LOG"; out="$(WEIXUE_BACKUP_DIR="$bk" "$root/firmware/flash.sh" "$port" 2>&1)"; rc=$?
check "backup: flash.sh accepts the new backup" '[ $rc -eq 0 ] && wrote && echo "$out" | grep -q "^backup: $nb$"'
bk2="$tmp/bk2"
out="$(FAKE_MAC_AFTER_READ="$other" WEIXUE_BACKUP_DIR="$bk2" "$root/scripts/backup_flash.sh" "$port" 2>&1)"; rc=$?
check "backup: MAC change during the read refused, partial removed" '[ $rc -ne 0 ] && echo "$out" | grep -q "MAC changed" && [ -z "$(ls -A "$bk2")" ] && nolock'
echo "$mac" > "$FAKE_MAC_FILE"

# adopt_backup.sh
ad="$tmp/adopt"; mkdir -p "$ad"
a1="$ad/flash-20250101-000001.bin"
cp "$FAKE_DEVICE" "$a1"
run "$root/scripts/adopt_backup.sh" "$a1" "$port"
check "adopt: matching board:uuid tags the backup" '[ $rc -eq 0 ] && [ "$(cat "${a1%.bin}.mac")" = "$mac" ] && (cd "$ad" && shasum -a 256 -c --quiet "$(basename "${a1%.bin}.sha256")") && nolock'
check "adopt: files 0600" '[ "$(mode "$a1")" = 600 ] && [ "$(mode "${a1%.bin}.mac")" = 600 ] && [ "$(mode "${a1%.bin}.sha256")" = 600 ]'
run "$root/scripts/adopt_backup.sh" "$a1" "$port"
check "adopt: already tagged refused" '[ $rc -ne 0 ] && echo "$out" | grep -q "already tagged"'
: > "$FAKE_LOG"; out="$(WEIXUE_BACKUP_DIR="$ad" "$root/firmware/flash.sh" "$port" 2>&1)"; rc=$?
check "adopt: flash.sh accepts the adopted backup" '[ $rc -eq 0 ] && wrote && echo "$out" | grep -q "^backup: $a1$"'
a2="$ad/flash-20250101-000002.bin"; cp "$tmp/other.bin" "$a2"
run "$root/scripts/adopt_backup.sh" "$a2" "$port"
check "adopt: other board:uuid refused, untagged" '[ $rc -ne 0 ] && echo "$out" | grep -q "board:uuid differs" && [ ! -e "${a2%.bin}.mac" ] && nolock'
a3="$ad/flash-20250101-000003.bin"; head -c 1048576 "$FAKE_DEVICE" > "$a3"
run "$root/scripts/adopt_backup.sh" "$a3" "$port"
check "adopt: short backup refused" '[ $rc -ne 0 ] && echo "$out" | grep -q "not a full" && [ ! -e "${a3%.bin}.mac" ]'
a4="$ad/flash-20250101-000004.bin"; cp "$FAKE_DEVICE" "$a4"; digest "$a4"; flip "$a4"
run "$root/scripts/adopt_backup.sh" "$a4" "$port"
check "adopt: digest mismatch refused" '[ $rc -ne 0 ] && echo "$out" | grep -q "sha256 .* differs" && [ ! -e "${a4%.bin}.mac" ]'
a5="$ad/flash-20250101-000005.bin"; cp "$tmp/none.bin" "$a5"
run "$root/scripts/adopt_backup.sh" "$a5" "$port"
check "adopt: backup without board:uuid refused" '[ $rc -ne 0 ] && echo "$out" | grep -q "has no NVS board:uuid" && [ ! -e "${a5%.bin}.mac" ]'
a6="$ad/flash-20250101-000006.bin"; cp "$FAKE_DEVICE" "$a6"
out="$(FAKE_DEVICE="$tmp/none.bin" "$root/scripts/adopt_backup.sh" "$a6" "$port" 2>&1)"; rc=$?
check "adopt: device without board:uuid refused" '[ $rc -ne 0 ] && echo "$out" | grep -q "device NVS has no board:uuid" && [ ! -e "${a6%.bin}.mac" ] && nolock'

echo "failures: $fails"
[ "$fails" -eq 0 ]
