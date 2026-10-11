#!/usr/bin/env bash
# Device session driver: under the device lock, builds and flashes the diag build, runs
# scripts/audio_session.py (Google TTS clips played with afplay for the voice items, plus injected
# gestures and touches, results in docs/logs/session-<date>.md), then builds and flashes the release
# build back.
#
#   scripts/audio_session.sh [--no-audio] [--no-flash] [--synth-only] [--port <port>] [-- <audio_session.py options>]
#
# --no-audio: gesture and touch items only; no clips are synthesised or played and the Mac volume is not touched.
# --no-flash: runs against the firmware already on the board and leaves it there.
# --synth-only: synthesises and caches every clip (scripts/session_tts.py), then exits; no board, lock or volume change.
# Audio mode needs home mode (this Mac's en0 address equals LAN_IP in the live server/.env) and
# sets the Mac output volume to 55 for the run, restoring volume and mute state on exit. Clips are
# synthesised with the service account at GOOGLE_SA_FILE in the live server/.env (exported as
# GOOGLE_APPLICATION_CREDENTIALS) before the board is touched.
# From the same .env: WEIXUE_PROACTIVE_TOKEN is exported for card_tap; the fixture base is
# http://<BIND_IP, or 127.0.0.1 for 0.0.0.0>:8003 and the fixture device the first ALLOWED_DEVICES
# entry; VOICE_BARGE_IN (default 0) is passed as --barge-in. Options after -- override these.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
live_env="${WEIXUE_LIVE_ENV:-$HOME/Projects/weixue/server/.env}"
volume=55
no_audio=0
synth_only=0
flash=1
port_arg=""
py_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-audio) no_audio=1 ;;
    --no-flash) flash=0 ;;
    --synth-only) synth_only=1 ;;
    --port) port_arg="$2"; shift ;;
    --) shift; py_args=("$@"); break ;;
    *) echo "usage: $0 [--no-audio] [--no-flash] [--synth-only] [--port <port>] [-- <audio_session.py options>]" >&2; exit 2 ;;
  esac
  shift
done

# shellcheck source=scripts/device.sh
. "$root/scripts/device.sh"

# Value of KEY in the live .env (last assignment, surrounding quotes removed); empty when unset.
env_value() {
  sed -n "s/^$1=//p" "$live_env" 2>/dev/null | tail -1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# Exports GOOGLE_APPLICATION_CREDENTIALS from the live .env and warms the clip cache.
synth_clips() {
  local sa
  sa="$(env_value GOOGLE_SA_FILE)"
  [ -n "$sa" ] && [ -f "$sa" ] || die "GOOGLE_SA_FILE in $live_env is unset or not a file"
  export GOOGLE_APPLICATION_CREDENTIALS="$sa"
  "$root/.venv/bin/python" -c 'import google.cloud.texttospeech' 2>/dev/null || {
    local pin
    pin="$(grep '^google-cloud-texttospeech==' "$root/server/requirements-overlay.txt")"
    [ -n "$pin" ] || die "no google-cloud-texttospeech pin in server/requirements-overlay.txt"
    "$root/.venv/bin/pip" install -q "$pin"
  }
  "$root/.venv/bin/python" "$root/scripts/audio_session.py" --synth-only ${py_args[@]+"${py_args[@]}"}
}

if [ "$synth_only" = 1 ]; then
  synth_clips
  exit
fi

device_select_port "$port_arg"

if [ "$no_audio" = 0 ]; then
  lan_ip="$(env_value LAN_IP)"
  here="$(ipconfig getifaddr en0 2>/dev/null || true)"
  [ -n "$lan_ip" ] && [ "$here" = "$lan_ip" ] \
    || die "not in home mode (en0 ${here:-none}, LAN_IP ${lan_ip:-unset}); audio is off in office mode, use --no-audio"
  command -v afplay >/dev/null || die "afplay not found"
  synth_clips
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
token="$(env_value WEIXUE_PROACTIVE_TOKEN)"
[ -n "$token" ] && export WEIXUE_PROACTIVE_TOKEN="$token"
unset token
bind_ip="$(env_value BIND_IP)"
[ -z "$bind_ip" ] || [ "$bind_ip" = 0.0.0.0 ] && bind_ip=127.0.0.1
mode_args+=(--fixture-base "http://$bind_ip:8003")
device_id="$(env_value ALLOWED_DEVICES | cut -d, -f1 | tr -d ' ')"
[ -n "$device_id" ] && mode_args+=(--fixture-device "$device_id")
barge_in="$(env_value VOICE_BARGE_IN)"
mode_args+=(--barge-in "${barge_in:-0}")
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
