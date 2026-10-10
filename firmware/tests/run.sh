#!/usr/bin/env bash
# Builds and runs the host tests for firmware/overrides sources and the patched vendored MCP server
# (the agent and MCP tests link cJSON from a firmware/build.sh scratch copy's managed components)
# and the firmware/tools scripts.
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
for test in agent_msg_test agent_store_test agent_ui_test agent_outbox_test; do
  c++ "${flags[@]}" \
    -I "$root/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75" \
    -isystem "$cjson" \
    "$root/firmware/tests/$test.cc" "$out/cJSON.o" -o "$out/$test"
  "$out/$test"
done
# The vendored MCP server with 34-mcp-tool-visibility.patch, against host stubs.
src="$vendor/xiaozhi-esp32"
mkdir -p "$out/mcp/main"
cp "$src/main/mcp_server.h" "$src/main/mcp_server.cc" "$src/main/cjson_utils.h" "$out/mcp/main/"
(cd "$out/mcp" && git apply "$root/firmware/patches/34-mcp-tool-visibility.patch")
mcp_flags=("${flags[@]/-std=c++17/-std=c++23}")
c++ "${mcp_flags[@]}" -Wno-unused-parameter -Wno-unused-lambda-capture -DBOARD_NAME='"host"' \
  -I "$root/firmware/tests/mcp_stubs" -I "$out/mcp/main" -I "$root/firmware/overrides/main" \
  -isystem "$cjson" \
  "$out/mcp/main/mcp_server.cc" "$root/firmware/tests/mcp_policy_test.cc" "$out/cJSON.o" \
  -o "$out/mcp_policy_test"
"$out/mcp_policy_test"
"$py" -m unittest discover -s "$root/firmware/tests" -p 'test_*.py'
