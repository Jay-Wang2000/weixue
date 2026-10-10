#!/usr/bin/env bash
# Device session driver: under the device lock, builds and flashes the diag build, runs
# scripts/audio_session.py (say-driven voice items plus injected gestures and touches, results in
# docs/logs/session-<date>.md), then builds and flashes the release build back.
#
#   scripts/audio_session.sh [--no-audio] [--no-flash] [--port <port>] [-- <audio_session.py options>]
#
# --no-audio: gesture and touch items only; `say` and the Mac volume are not touched.
# --no-flash: runs against the firmware already on the board and leaves it there.
# Audio mode needs home mode (this Mac's en0 address equals LAN_IP in the live server/.env) and
# sets the Mac output volume to 55 for the run, restoring volume and mute state on exit.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
live_env="${WEIXUE_LIVE_ENV:-$HOME/Projects/weixue/server/.env}"
volume=55
no_audio=0
flash=1
port_arg=""
py_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-audio) no_audio=1 ;;
    --no-flash) flash=0 ;;
    --port) port_arg="$2"; shift ;;
    --) shift; py_args=("$@"); break ;;
    *) echo "usage: $0 [--no-audio] [--no-flash] [--port <port>] [-- <audio_session.py options>]" >&2; exit 2 ;;
  esac
  shift
done

# shellcheck source=scripts/device.sh
. "$root/scripts/device.sh"
device_select_port "$port_arg"

if [ "$no_audio" = 0 ]; then
  lan_ip="$(sed -n 's/^LAN_IP=//p' "$live_env" 2>/dev/null | tail -1)"
  here="$(ipconfig getifaddr en0 2>/dev/null || true)"
  [ -n "$lan_ip" ] && [ "$here" = "$lan_ip" ] \
    || die "not in home mode (en0 ${here:-none}, LAN_IP ${lan_ip:-unset}); audio is off in office mode, use --no-audio"
  command -v say >/dev/null || die "macOS say not found"
fi

stamp="$(date -u +%Y-%m-%d-%H%M)"
logs="$root/docs/logs"
mkdir -p "$logs"
build_log="$logs/session-$stamp-build.log"

old_volume=""
old_muted=""
restore_volume() {
  [ -n "$old_volume" ] || return 0
  osascript -e "set volume output volume $old_volume" -e "set volume output muted $old_muted" || true
  echo "volume: restored $old_volume (muted $old_muted)"
}
device_lock_cleanup=restore_volume
device_lock_acquire audio_session.sh

if [ "$no_audio" = 0 ]; then
  old_volume="$(osascript -e 'output volume of (get volume settings)')"
  old_muted="$(osascript -e 'output muted of (get volume settings)')"
  osascript -e "set volume output volume $volume" -e "set volume output muted false"
  echo "volume: $volume (was $old_volume, muted $old_muted)"
fi

if [ "$flash" = 1 ]; then
  echo "building diag build (log: $build_log)"
  WEIXUE_FACE_DIAG=1 "$root/firmware/build.sh" >> "$build_log" 2>&1 || die "diag build failed; see $build_log"
  "$root/firmware/flash.sh" "$port" >> "$build_log" 2>&1 || die "diag flash failed; see $build_log"
  echo "flashed diag build"
fi

mode_args=()
[ "$no_audio" = 1 ] && mode_args+=(--no-audio)
set +e
"$root/.venv/bin/python" "$root/scripts/audio_session.py" --port "$port" \
  --out "$logs/session-$stamp.md" --build-info "$(git -C "$root" rev-parse --short HEAD) diag" \
  ${mode_args[@]+"${mode_args[@]}"} ${py_args[@]+"${py_args[@]}"}
status=$?
set -e

if [ "$flash" = 1 ]; then
  echo "building release build"
  "$root/firmware/build.sh" >> "$build_log" 2>&1 || die "release build failed; see $build_log (the board still runs the diag build)"
  "$root/firmware/flash.sh" "$port" >> "$build_log" 2>&1 || die "release flash failed; see $build_log (the board still runs the diag build)"
  echo "flashed release build"
fi
echo "session status: $status (table: docs/logs/session-$stamp.md)"
exit "$status"
