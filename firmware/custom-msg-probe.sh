#!/usr/bin/env bash
# Runs firmware/tools/custom_msg_probe.py on port 8013 in the backend image (which has aiohttp); extra args go to the probe.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
image="${WEIXUE_SERVER_IMAGE:-weixue/xiaozhi-esp32-server:local}"
exec docker run --rm --name weixue-custom-msg-probe -p 8013:8013 \
  -v "$root/firmware/tools/custom_msg_probe.py:/probe.py:ro" \
  --entrypoint python "$image" -u /probe.py "$@"
