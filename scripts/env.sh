# Source this file: `source scripts/env.sh`. Returns non-zero without exporting ESP-IDF when the ESP-IDF checkout is not the clean vendor.lock pin, or when the cmake/ninja that ESP-IDF export.sh selects (checked in a subshell first) differ from their vendor.lock pins.
_weixue_vendor="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}"
_weixue_lock="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")/.." && pwd)/vendor.lock"
_weixue_pin() { awk -v n="$1" '$1==n{print $3}' "$_weixue_lock"; }
_weixue_env_fail() { echo "error: $*" >&2; unset -f _weixue_pin _weixue_env_fail; unset _weixue_vendor _weixue_lock _weixue_idf _weixue_tools; }
_weixue_idf="$(_weixue_pin esp-idf)"
if [ -z "$_weixue_idf" ]; then
  _weixue_env_fail "vendor.lock has no esp-idf pin"; return 1
fi
if [ "$(git -C "$_weixue_vendor/esp-idf" rev-parse HEAD 2>/dev/null)" != "$(git -C "$_weixue_vendor/esp-idf" rev-parse "$_weixue_idf^{commit}" 2>/dev/null)" ]; then
  _weixue_env_fail "$_weixue_vendor/esp-idf is not at vendor.lock esp-idf $_weixue_idf"; return 1
fi
if [ -n "$(git -C "$_weixue_vendor/esp-idf" status --porcelain 2>&1)" ]; then
  _weixue_env_fail "$_weixue_vendor/esp-idf has local changes"; return 1
fi
_weixue_tools="$(
  export IDF_TOOLS_PATH="$_weixue_vendor/.espressif" IDF_PATH="$_weixue_vendor/esp-idf"
  . "$IDF_PATH/export.sh" >/dev/null || exit 1
  echo "$(cmake --version | awk 'NR==1{print $3}') $(ninja --version)"
)" || { _weixue_env_fail "ESP-IDF export.sh failed"; return 1; }
if [ "${_weixue_tools% *}" != "$(_weixue_pin cmake)" ]; then
  _weixue_env_fail "export.sh puts cmake ${_weixue_tools% *} on PATH, vendor.lock pins $(_weixue_pin cmake)"; return 1
fi
if [ "${_weixue_tools#* }" != "$(_weixue_pin ninja)" ]; then
  _weixue_env_fail "export.sh puts ninja ${_weixue_tools#* } on PATH, vendor.lock pins $(_weixue_pin ninja)"; return 1
fi
export IDF_TOOLS_PATH="$_weixue_vendor/.espressif"
export IDF_PATH="$_weixue_vendor/esp-idf"
export XIAOZHI_PATH="$_weixue_vendor/xiaozhi-esp32"
. "$IDF_PATH/export.sh" || { _weixue_env_fail "ESP-IDF export.sh failed"; return 1; }
echo "esp-idf $_weixue_idf ($(git -C "$IDF_PATH" rev-parse HEAD)), cmake $(_weixue_pin cmake), ninja $(_weixue_pin ninja)"
unset -f _weixue_pin _weixue_env_fail
unset _weixue_vendor _weixue_lock _weixue_idf _weixue_tools
