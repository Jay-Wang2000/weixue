# Source this file: `source scripts/env.sh`
_weixue_vendor="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}"
export IDF_TOOLS_PATH="$_weixue_vendor/.espressif"
export IDF_PATH="$_weixue_vendor/esp-idf"
export XIAOZHI_PATH="$_weixue_vendor/xiaozhi-esp32"
. "$IDF_PATH/export.sh"
unset _weixue_vendor
