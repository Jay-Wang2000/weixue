# Source this file with $root set to the repo root: creates or repairs $root/.venv with the vendor.lock esptool pin and sets $esptool.
_esptool_pin="$(awk '$1=="esptool"{print $3}' "$root/vendor.lock")"
[ -n "$_esptool_pin" ] || { echo "error: vendor.lock has no esptool pin" >&2; exit 1; }
[ -x "$root/.venv/bin/python" ] || python3 -m venv "$root/.venv"
if [ "$("$root/.venv/bin/python" -c 'import esptool; print(esptool.__version__)' 2>/dev/null)" != "$_esptool_pin" ]; then
  "$root/.venv/bin/pip" install -q "esptool==$_esptool_pin"
fi
esptool="$root/.venv/bin/esptool"
unset _esptool_pin
