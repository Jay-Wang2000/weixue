#!/usr/bin/env bash
# Prints the source identity of a firmware build from the current checkout and vendor tree: firmware/scripts/vendor.lock trees and uncommitted changes, vendored clone and ESP-IDF commits, board config, dependencies.lock, patches and overrides.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
vendor="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}"
inputs=(firmware scripts vendor.lock)

sha() { shasum -a 256 "$1" | awk '{print $1}'; }

echo "inputs $(git -C "$root" rev-parse "${inputs[@]/#/HEAD:}" | tr '\n' ' ' | sed 's/ $//')"
changes="$({ git -C "$root" diff HEAD --binary -- "${inputs[@]}"
            git -C "$root" ls-files -o --exclude-standard -z -- "${inputs[@]}" \
              | (cd "$root" && xargs -0 shasum -a 256 2>/dev/null) || true; } | shasum -a 256 | awk '{print $1}')"
[ -n "$(git -C "$root" status --porcelain -- "${inputs[@]}")" ] || changes=none
echo "uncommitted $changes"
echo "xiaozhi-esp32 $(git -C "$vendor/xiaozhi-esp32" rev-parse HEAD)"
echo "esp-idf $(git -C "$vendor/esp-idf" rev-parse HEAD)"
echo "board $(sha "$root/firmware/board/config.weixue.json")"
echo "dependencies.lock $(sha "$root/firmware/dependencies.lock")"
shopt -s nullglob
for p in "$root"/firmware/patches/*.patch; do echo "patch $(sha "$p") $(basename "$p")"; done
if [ -d "$root/firmware/overrides" ]; then
  (cd "$root/firmware/overrides" && find . -type f | sed 's|^\./||' | LC_ALL=C sort) \
    | while IFS= read -r f; do echo "override $(sha "$root/firmware/overrides/$f") $f"; done
fi
