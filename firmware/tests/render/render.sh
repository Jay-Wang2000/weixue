#!/usr/bin/env bash
# Renders the agent UI screens on the host with the firmware's LVGL, fonts and agent_view.cc into
# PNGs and prints each screen's lit share: firmware/tests/render/render.sh [out_dir]. Needs a
# firmware/build.sh scratch copy (managed components: LVGL, xiaozhi-fonts, cJSON). The device's
# assets fonts (.bin) hold 32-bit structures, so the host uses the compiled basic 30/20 px fonts:
# Latin text, no Chinese sublabels.
set -euo pipefail
root="$(cd "$(dirname "$0")/../../.." && pwd)"
out="${1:-$root/docs/logs/ui-agent-states-png}"
vendor="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}"
mc="$(ls -d "$vendor"/fw-work/*/managed_components 2>/dev/null | head -1 || true)"
[ -n "$mc" ] || { echo "error: no managed_components under $vendor/fw-work (run firmware/build.sh once)" >&2; exit 1; }
lvgl="$mc/lvgl__lvgl"
fonts="$mc/78__xiaozhi-fonts"
cjson="$mc/espressif__cjson/cJSON"
board="$root/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75"
obj="${WEIXUE_RENDER_OBJ:-${TMPDIR:-/tmp}/weixue-agent-render}"
mkdir -p "$out" "$obj/lvgl"
cflags=(-O1 -DLV_CONF_INCLUDE_SIMPLE -I "$root/firmware/tests/render" -I "$lvgl" -I "$lvgl/src")
if [ ! -f "$obj/liblvgl.a" ]; then
  find "$lvgl/src" -name '*.c' | xargs -P 8 -I{} sh -c \
    'f="$1"; d="$2"; shift 2; cc "$@" -w -c "$f" -o "$d/$(printf %s "$f" | shasum | cut -c1-16).o"' \
    sh {} "$obj/lvgl" "${cflags[@]}"
  ar rcs "$obj/liblvgl.a" "$obj"/lvgl/*.o
fi
cc "${cflags[@]}" -c "$fonts/src/font_noto_sans_basic_20_4.c" -o "$obj/font20.o"
cc "${cflags[@]}" -c "$fonts/src/font_noto_sans_basic_30_4.c" -o "$obj/font30.o"
cc -std=c99 -O1 -c "$cjson/cJSON.c" -o "$obj/cJSON.o"
cxx=(c++ -std=c++17 -O1 -Wall -Wextra)
if [ "$(uname)" = Darwin ]; then
  cxx+=(-nostdinc++ -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1")
fi
"${cxx[@]}" "${cflags[@]}" -I "$board" -isystem "$cjson" \
  "$root/firmware/tests/render/agent_render.cc" "$board/agent_view.cc" \
  "$obj/font20.o" "$obj/font30.o" "$obj/cJSON.o" "$obj/liblvgl.a" -lz -o "$obj/agent_render"
"$obj/agent_render" "$out"
