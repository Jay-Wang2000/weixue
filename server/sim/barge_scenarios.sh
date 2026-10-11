#!/bin/bash
# Barge-in flag rounds (docker-compose.barge.yml): scripted ASR -> SimLLM (three-sentence reply) -> tone TTS.
#   ./barge_scenarios.sh [TRANSCRIPT]
# For VOICE_BARGE_IN=0 and then 1 the sim server is re-rendered (scripts/render_config.py, barge.config.yaml) and
# recreated, then: during, an utterance 1 s into the reply's audio; cue, an utterance that starts during the wake
# cue. Prints one PASS/FAIL line per check; the transcript (sim device output and server log) goes to TRANSCRIPT.
# Needs the repo's .venv and the local server image. Nothing outside the weixue-barge-sim compose project is
# started, stopped or recreated.
cd "$(dirname "$0")"
export PATH="$HOME/.rd/bin:$PATH"
ROOT=$(cd ../.. && pwd)
PY="$ROOT/.venv/bin/python"
export DYLD_FALLBACK_LIBRARY_PATH="$ROOT/.venv/lib"
SIMDEV="$ROOT/server/scripts/proactive_sim_device.py"
COMPOSE=(docker compose -f docker-compose.barge.yml)
CONTAINER=weixue-barge-sim-server
WS=ws://127.0.0.1:18300/xiaozhi/v1/
BASE=http://127.0.0.1:18303
DEV=aa:bb:cc:dd:ee:03
CLIP="$ROOT/server/tests/assets/en-US_turn_on_the_living_room_lights.wav"
TRANSCRIPT=${1:-$PWD/barge-$(date +%s).out}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"; "${COMPOSE[@]}" down >/dev/null 2>&1' EXIT
PASSES=0
FAILS=0
: > "$TRANSCRIPT"
mkdir -p data/barge

check() {
  local what=$1
  shift
  if "$@"; then PASSES=$((PASSES + 1)); echo "PASS $what"; else FAILS=$((FAILS + 1)); echo "FAIL $what"; fi
}
has() { grep -qE -- "$2" "$1"; }
lacks() { ! grep -qE -- "$2" "$1"; }
# reply_frames FILE: audio frames between the first and the second `tts stop` (the reply after the cue).
reply_frames() {
  grep -E '"state": "stop"' "$1" | sed -nE 's/.*audio frames so far ([0-9]+).*/\1/p' | head -2 | paste -sd' ' - \
    | awk '{print $2 - $1}'
}
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

start() {  # start 0|1: the sim server rendered for VOICE_BARGE_IN
  VOICE_BARGE_IN=$1 python3 "$ROOT/server/scripts/render_config.py" barge.config.yaml > data/barge/.config.yaml \
    || { echo "FAIL render VOICE_BARGE_IN=$1"; exit 1; }
  "${COMPOSE[@]}" up -d --force-recreate >> "$TRANSCRIPT" 2>&1 || { echo "FAIL compose up"; exit 1; }
  for _ in $(seq 1 60); do
    "$PY" -c "import socket; socket.create_connection(('127.0.0.1', 18300), 1)" 2>/dev/null && break
    sleep 1
  done
  sleep 2
}

round() {  # round NAME TEXT [SIM ARGS...]: one sim-device session; the lines of TEXT are its transcripts in order
  local name=$1 text=$2
  shift 2
  printf '%s\n' "$text" > data/barge/sim_transcripts.txt
  local since
  since=$(now)
  "$PY" "$SIMDEV" --base $BASE --ws $WS --device $DEV --direct --wake Jarvis --reply "$CLIP" --reply-delay 0.3 \
    --max-seconds 40 "$@" > "$WORK/$name" 2>&1
  sleep 1
  docker logs --since "$since" $CONTAINER > "$WORK/$name.log" 2>&1
  { echo "## $name: $text"; echo "-- sim device"; cat "$WORK/$name"; echo "-- server log";
    grep -E '大模型收到|barge-in|playback echo|Abort message received$|wake cue' "$WORK/$name.log"; } >> "$TRANSCRIPT"
}

LONG="Tell me something long."
BARGE="Stop, I have another question."
AFTER="What time is it?"
# The tone reply of barge.config.yaml is 90 frames (sim_main.py: 0.06 s per character, at most 4 s a segment).
FULL=85

for flag in 0 1; do
  start $flag
  echo "-- VOICE_BARGE_IN=$flag" | tee -a "$TRANSCRIPT"
  check "barge_in=$flag: rendered" grep -q "^barge_in: $([ $flag = 1 ] && echo true || echo false)$" data/barge/.config.yaml

  if [ $flag = 0 ]; then
    # Cue stop -> LONG; 1 s into its audio -> BARGE; its tts stop -> AFTER.
    round during "$(printf '%s\n%s\n%s' "$LONG" "$BARGE" "$AFTER")" --replies 2 --barge-in 1
    check "off: the utterance during playback was dropped" has "$WORK/during.log" \
      "barge-in disabled: dropped utterance during playback: '$BARGE'"
    check "off: no abort" lacks "$WORK/during.log" "Abort message received$"
    check "off: the dropped utterance reached no LLM" lacks "$WORK/during.log" "大模型收到用户消息: $BARGE"
    check "off: the reply played to its end ($(reply_frames "$WORK/during") frames)" \
      test "$(reply_frames "$WORK/during")" -ge $FULL
    check "off: the utterance after playback was answered" has "$WORK/during.log" "大模型收到用户消息: $AFTER"
  else
    round during "$(printf '%s\n%s' "$LONG" "$BARGE")" --replies 1 --barge-in 1
    check "on: the utterance during playback aborted the reply" has "$WORK/during.log" "Abort message received$"
    check "on: the utterance started a turn" has "$WORK/during.log" "大模型收到用户消息: $BARGE"
    check "on: nothing dropped by the flag" lacks "$WORK/during.log" "barge-in disabled"
    check "on: the reply was cut ($(reply_frames "$WORK/during") frames)" test "$(reply_frames "$WORK/during")" -lt $FULL
  fi

  # The utterance starts at the cue's first audio frame.
  round cue "$AFTER" --replies 0 --reply-during-playback --reply-delay 0
  check "barge_in=$flag: the wake cue played" has "$WORK/cue.log" "wake cue: "
  check "barge_in=$flag: speech during the cue started a turn" has "$WORK/cue.log" "大模型收到用户消息: $AFTER"
  check "barge_in=$flag: speech during the cue was not dropped" lacks "$WORK/cue.log" "barge-in disabled"
done

echo "$PASSES passed, $FAILS failed (transcript: $TRANSCRIPT)"
[ "$FAILS" -eq 0 ]
