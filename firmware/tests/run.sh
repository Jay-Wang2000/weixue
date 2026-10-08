#!/usr/bin/env bash
# Builds and runs the host tests for firmware/overrides sources (the agent tests link cJSON from a
# firmware/build.sh scratch copy's managed components) and firmware/tools/usb_provision.py.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
flags=(-std=c++17 -Wall -Wextra -Werror)
# Command Line Tools ship libc++ headers in the SDK; the toolchain's own copy can be incomplete.
if [ "$(uname)" = Darwin ]; then
  flags+=(-nostdinc++ -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1")
fi
for test in imu_gestures_test ring_bedside_test clock_test session_test; do
  c++ "${flags[@]}" \
    -I "$root/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75" \
    -I "$root/firmware/overrides/main" \
    "$root/firmware/tests/$test.cc" -o "$out/$test"
  "$out/$test"
done
py="$root/.venv/bin/python"
[ -x "$py" ] || py=python3
vendor="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}"
cjson="$(ls -d "$vendor"/fw-work/*/managed_components/espressif__cjson/cJSON 2>/dev/null | head -1 || true)"
[ -n "$cjson" ] || { echo "error: no espressif__cjson under $vendor/fw-work (run firmware/build.sh once)" >&2; exit 1; }
cc -std=c99 -O1 -c "$cjson/cJSON.c" -o "$out/cJSON.o"
for test in agent_msg_test agent_store_test agent_ui_test; do
  c++ "${flags[@]}" \
    -I "$root/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75" \
    -isystem "$cjson" \
    "$root/firmware/tests/$test.cc" "$out/cJSON.o" -o "$out/$test"
  "$out/$test"
done
"$py" -m unittest discover -s "$root/firmware/tests" -p 'test_*.py'
