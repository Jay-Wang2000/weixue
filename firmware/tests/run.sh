#!/usr/bin/env bash
# Builds and runs the host tests for firmware/overrides sources.
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
flags=(-std=c++17 -Wall -Wextra -Werror)
# Command Line Tools ship libc++ headers in the SDK; the toolchain's own copy can be incomplete.
if [ "$(uname)" = Darwin ]; then
  flags+=(-nostdinc++ -isystem "$(xcrun --show-sdk-path)/usr/include/c++/v1")
fi
c++ "${flags[@]}" \
  -I "$root/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75" \
  "$root/firmware/tests/imu_gestures_test.cc" -o "$out/imu_gestures_test"
"$out/imu_gestures_test"
