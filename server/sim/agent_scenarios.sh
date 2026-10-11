#!/bin/bash
# claude_agent sim rounds (docker-compose.agent.yml): scripted ASR -> claude_agent -> agent-service -> tone TTS.
#   ./agent_scenarios.sh live [TRANSCRIPT]    the deployed agent-service: "What time is it?"
#   ./agent_scenarios.sh branch [TRANSCRIPT]  this tree's agent-service: the same, then "Set bedside mode."
#                                             (device_action -> the sim device's MCP tools/call), then the
#                                             service stopped (fallback line)
# Keys come from ENV_FILE (default: the live server/.env) and are never printed. Prints one PASS/FAIL line
# per check; the transcript (sim device output and server log) goes to TRANSCRIPT. Needs the repo's .venv.
cd "$(dirname "$0")"
export PATH="$HOME/.rd/bin:$PATH"
MODE=${1:?usage: agent_scenarios.sh live|branch [TRANSCRIPT]}
ROOT=$(cd ../.. && pwd)
PY="$ROOT/.venv/bin/python"
export DYLD_FALLBACK_LIBRARY_PATH="$ROOT/.venv/lib"
SIMDEV="$ROOT/server/scripts/proactive_sim_device.py"
ENV_FILE=${ENV_FILE:-$HOME/Projects/weixue/server/.env}
CONTAINER=weixue-agent-sim-server
WS=ws://127.0.0.1:18200/xiaozhi/v1/
BASE=http://127.0.0.1:18203
DEV=aa:bb:cc:dd:ee:01
CLIP="$ROOT/server/tests/assets/en-US_turn_on_the_living_room_lights.wav"
TRANSCRIPT=${2:-$PWD/agent-$MODE-$(date +%s).out}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PASSES=0
FAILS=0
: > "$TRANSCRIPT"

env_value() { (set -a; . "$ENV_FILE"; set +a; printf '%s' "${!1}"); }
check() {
  local what=$1
  shift
  if "$@"; then PASSES=$((PASSES + 1)); echo "PASS $what"; else FAILS=$((FAILS + 1)); echo "FAIL $what"; fi
}
has() { grep -qE -- "$2" "$1"; }

case "$MODE" in
  live)
    export SIM_AGENT_URL=http://agent-service:8010
    SIM_AGENT_TOKEN=$(env_value AGENT_SERVICE_TOKEN)
    PROFILE=()
    ;;
  branch)
    export SIM_AGENT_URL=http://sim-agent-service:8010
    SIM_AGENT_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    ANTHROPIC_API_KEY=$(env_value ANTHROPIC_API_KEY)
    export ANTHROPIC_API_KEY
    PROFILE=(--profile branch)
    mkdir -p data/agent-service
    ;;
  *) echo "usage: agent_scenarios.sh live|branch [TRANSCRIPT]"; exit 2 ;;
esac
export SIM_AGENT_TOKEN
[ -n "$SIM_AGENT_TOKEN" ] || { echo "FAIL no agent-service token"; exit 1; }

docker compose -f docker-compose.agent.yml "${PROFILE[@]}" up -d --build --force-recreate >> "$TRANSCRIPT" 2>&1 \
  || { echo "FAIL compose up"; exit 1; }
for _ in $(seq 1 60); do
  "$PY" -c "import socket; socket.create_connection(('127.0.0.1', 18200), 1)" 2>/dev/null && break
  sleep 1
done
if [ "$MODE" = branch ]; then
  for _ in $(seq 1 60); do
    docker exec weixue-agent-sim-service python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=2)" 2>/dev/null && break
    sleep 1
  done
fi
sleep 2

round() {  # round NAME TEXT [SIM ARGS...]
  local name=$1 text=$2
  shift 2
  printf '%s\n' "$text" > data/sim_transcripts.txt
  local since
  since=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  "$PY" "$SIMDEV" --base $BASE --ws $WS --device $DEV --direct --wake Jarvis --reply "$CLIP" --reply-delay 0.3 \
    --max-seconds 30 "$@" > "$WORK/$name" 2>&1
  sleep 1
  docker logs --since "$since" $CONTAINER > "$WORK/$name.log" 2>&1
  { echo "## $name: $text"; echo "-- sim device"; cat "$WORK/$name"; echo "-- server log";
    grep -E 'turn_latency|claude_agent|device_action|Google TTS first audio|大模型收到|识别文本|breaker' "$WORK/$name.log"; } >> "$TRANSCRIPT"
}

round time "What time is it?"
check "time: the turn reached the provider" has "$WORK/time.log" "大模型收到用户消息: What time is it"
check "time: one turn_latency line ending at turn_done" has "$WORK/time.log" "turn_latency .*reply_end=turn_done"
check "time: service, TTS and total timings measured" \
  has "$WORK/time.log" "turn_latency .*service_first_text_ms=[0-9]+ tts_first_audio_ms=[0-9]+ total_ms=[0-9]+"
check "time: the reply was captioned and played" has "$WORK/time" '"state": "sentence_start"'
check "time: no fallback line" sh -c "! grep -qE 'can.t reach my assistant|lost the connection' '$WORK/time'"
grep -E 'turn_latency' "$WORK/time.log" | sed 's/^.*turn_latency/turn_latency/'
grep -oE '"state": "sentence_start", "text": "[^"]*"' "$WORK/time" | head -2

if [ "$MODE" = branch ]; then
  round bedside "Set bedside mode." --mcp-tools
  check "bedside: device_action reached the device MCP" has "$WORK/bedside" 'mcp call self.screen.set_bedside .*"mode": "night"'
  check "bedside: the gateway acked the result" has "$WORK/bedside.log" "device_action self.screen.set_bedside ok=True"
  check "bedside: no operator-only tool was called" sh -c "! grep -q 'mcp call self.diag' '$WORK/bedside'"
  check "bedside: turn_latency line" has "$WORK/bedside.log" "turn_latency .*reply_end="
  grep -E 'turn_latency' "$WORK/bedside.log" | sed 's/^.*turn_latency/turn_latency/'
  grep -oE '"state": "sentence_start", "text": "[^"]*"' "$WORK/bedside" | head -3

  docker stop weixue-agent-sim-service >> "$TRANSCRIPT" 2>&1
  round down "Are you there?"
  check "down: the fallback line is spoken" has "$WORK/down" '"state": "sentence_start", "text": "I can.t reach my assistant right now"'
  check "down: turn_latency reports the fallback" has "$WORK/down.log" "turn_latency .*reply_end=fallback"
  grep -E 'turn_latency' "$WORK/down.log" | sed 's/^.*turn_latency/turn_latency/'
fi

echo "$PASSES passed, $FAILS failed (transcript: $TRANSCRIPT)"
[ "$FAILS" -eq 0 ]
