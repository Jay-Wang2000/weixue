#!/bin/bash
# claude_agent sim rounds (docker-compose.agent.yml): scripted ASR -> claude_agent -> agent-service -> tone TTS.
#   ./agent_scenarios.sh live [TRANSCRIPT]    the deployed agent-service: "What time is it?"
#   ./agent_scenarios.sh m2 [TRANSCRIPT [ROUND...]]
#                                             the deployed agent-service, each round in a new chat of the sim
#                                             device (default: all rounds): long, a voiceless task of 2-4
#                                             minutes that finishes while the sim device is offline and is spoken
#                                             WHEN_IDLE through the gateway once it polls again; steer, a long task
#                                             steered ("How is it going?") then stopped; barge, a barge-in while
#                                             the answer plays (stream close); rollback, VOICE_LLM=anthropic (one
#                                             turn on the legacy path). About 10 minutes.
#   ./agent_scenarios.sh branch [TRANSCRIPT]  this tree's agent-service: the "live" round, then "Set bedside mode."
#                                             (device_action -> the sim device's MCP tools/call), then the
#                                             service stopped (fallback line)
# The sim server's config is agent.config.yaml rendered by scripts/render_config.py (VOICE_LLM, default
# claude_agent; VOICE_BARGE_IN, default 1 here for the barge round). Keys come from ENV_FILE (default: the live server/.env) and are never printed. Prints one
# PASS/FAIL line per check; the transcript (sim device output and server log) goes to TRANSCRIPT. Needs the
# repo's .venv. Nothing outside the weixue-agent-sim compose project is started, stopped or recreated.
cd "$(dirname "$0")"
export PATH="$HOME/.rd/bin:$PATH"
MODE=${1:?usage: agent_scenarios.sh live|m2|branch [TRANSCRIPT]}
ROOT=$(cd ../.. && pwd)
PY="$ROOT/.venv/bin/python"
export DYLD_FALLBACK_LIBRARY_PATH="$ROOT/.venv/lib"
SIMDEV="$ROOT/server/scripts/proactive_sim_device.py"
ENV_FILE=${ENV_FILE:-$HOME/Projects/weixue/server/.env}
COMPOSE=(docker compose -f docker-compose.agent.yml)
CONTAINER=weixue-agent-sim-server
WS=ws://127.0.0.1:18200/xiaozhi/v1/
BASE=http://127.0.0.1:18203
# A device id of its own: concurrent sims against one agent-service keep separate chats, voice windows and deliveries.
DEV=aa:bb:cc:dd:ee:02
CLIP="$ROOT/server/tests/assets/en-US_turn_on_the_living_room_lights.wav"
TRANSCRIPT=${2:-$PWD/agent-$MODE-$(date +%s).out}
ROUNDS=("${@:3}")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PASSES=0
FAILS=0
: > "$TRANSCRIPT"
# Read-only Home Assistant history, one call at a time (LONG: 50 calls, SLOW: 35; a task stops at 60 model
# iterations).
LONG="For light.living_room_ceiling_lights, light.living_room_dining_ceiling_light, sun.sun, zone.home and the \
living room donut light, look at the history of each one for each of the last ten days, one entity and one day per \
tool call, one call at a time, waiting for each result before the next and noting in one short line what it showed, \
then summarize the daily patterns in two sentences. Don't save anything to memory, don't notify me before you're \
done and don't ask me anything."
SLOW="For light.living_room_ceiling_lights, light.living_room_dining_ceiling_light, sun.sun, zone.home and the \
living room donut light, look at the history of each one for each of the last seven days, one entity and one day per \
tool call, one call at a time, waiting for each result before the next, then summarize the daily patterns. Don't \
save anything to memory."

env_value() { (set -a; . "$ENV_FILE"; set +a; printf '%s' "${!1}"); }
check() {
  local what=$1
  shift
  if "$@"; then PASSES=$((PASSES + 1)); echo "PASS $what"; else FAILS=$((FAILS + 1)); echo "FAIL $what"; fi
}
has() { grep -qE -- "$2" "$1"; }
want() { [ ${#ROUNDS[@]} -eq 0 ] || [[ " ${ROUNDS[*]} " == *" $1 "* ]]; }
lacks() { ! grep -qE -- "$2" "$1"; }
since_logs() { docker logs --since "$1" $CONTAINER 2>&1; }
now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

case "$MODE" in
  live|m2)
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
  *) echo "usage: agent_scenarios.sh live|m2|branch [TRANSCRIPT]"; exit 2 ;;
esac
export SIM_AGENT_TOKEN
[ -n "$SIM_AGENT_TOKEN" ] || { echo "FAIL no agent-service token"; exit 1; }

render() {  # render VOICE_LLM: the sim server's config
  VOICE_LLM=$1 VOICE_BARGE_IN=${VOICE_BARGE_IN:-1} python3 "$ROOT/server/scripts/render_config.py" agent.config.yaml > data/agent.rendered.yaml
}
wait_server() {
  for _ in $(seq 1 60); do
    "$PY" -c "import socket; socket.create_connection(('127.0.0.1', 18200), 1)" 2>/dev/null && break
    sleep 1
  done
  sleep 2
}
api() {  # api METHOD PATH [JSON]: agent-service as the sim gateway (URL and token from the container environment)
  docker exec -i $CONTAINER python - "$@" <<'PY'
import json, os, sys, urllib.error, urllib.request, uuid
method, path = sys.argv[1], sys.argv[2]
body = sys.argv[3].encode() if len(sys.argv) > 3 else None
request = urllib.request.Request(os.environ["AGENT_SERVICE_URL"].rstrip("/") + path, data=body, method=method, headers={
    "Authorization": "Bearer " + os.environ["AGENT_SERVICE_TOKEN"], "Content-Type": "application/json",
    "Idempotency-Key": str(uuid.uuid4())})
try:
    print(urllib.request.urlopen(request, timeout=10).read().decode())
except urllib.error.HTTPError as e:
    print(json.dumps({"http_status": e.code}))
PY
}
field() { "$PY" -c "import json, sys; print(json.load(sys.stdin).get('$1') or '')"; }
# agent-service routes the device's next utterance to an open question of any chat for 20 s (VOICE_WINDOW_S).
new_chat() { sleep 21; api POST /v1/chats "{\"device_id\": \"$DEV\"}" | field chat_id; }
task_of() {  # task_of CHAT SINCE: the task of the chat's first agent card at the gateway since SINCE
  since_logs "$2" | grep -oE "agent item queued device=$DEV chat=$1 task=tsk_[0-9A-Z]+" | head -1 | sed 's/.*task=//'
}
wait_for_log() {  # wait_for_log SINCE PATTERN SECONDS
  local end=$(($(date +%s) + $3))
  while :; do
    since_logs "$1" | grep -qE -- "$2" && return 0
    [ "$(date +%s)" -lt "$end" ] || return 1
    sleep 5
  done
}

render claude_agent
"${COMPOSE[@]}" "${PROFILE[@]}" up -d --build --force-recreate >> "$TRANSCRIPT" 2>&1 \
  || { echo "FAIL compose up"; exit 1; }
wait_server
if [ "$MODE" = branch ]; then
  for _ in $(seq 1 60); do
    docker exec weixue-agent-sim-service python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=2)" 2>/dev/null && break
    sleep 1
  done
fi

round() {  # round NAME TEXT [SIM ARGS...]: one sim-device session; the lines of TEXT are its utterances in order
  local name=$1 text=$2
  shift 2
  printf '%s\n' "$text" > data/sim_transcripts.txt
  local since
  since=$(now)
  "$PY" "$SIMDEV" --base $BASE --ws $WS --device $DEV --direct --wake Jarvis --reply "$CLIP" --reply-delay 0.3 \
    --max-seconds 30 "$@" > "$WORK/$name" 2>&1
  sleep 1
  since_logs "$since" > "$WORK/$name.log"
  { echo "## $name: $text"; echo "-- sim device"; cat "$WORK/$name"; echo "-- server log";
    grep -E 'turn_latency|claude_agent|device_action|Google TTS first audio|大模型收到|识别文本|breaker|agent item queued|agent speech|llm.anthropic|playback echo' "$WORK/$name.log"; } >> "$TRANSCRIPT"
}
latency() { grep -E 'turn_latency' "$1" | sed 's/^.*turn_latency/turn_latency/'; }
said() { grep -oE '"state": "sentence_start", "text": "[^"]*"' "$1" | head -"${2:-3}"; }

if [ "$MODE" != m2 ]; then
  round time "What time is it?"
  check "time: the turn reached the provider" has "$WORK/time.log" "大模型收到用户消息: What time is it"
  check "time: one turn_latency line ending at turn_done" has "$WORK/time.log" "turn_latency .*reply_end=turn_done"
  check "time: service, TTS and total timings measured" \
    has "$WORK/time.log" "turn_latency .*service_first_text_ms=[0-9]+ tts_first_audio_ms=[0-9]+ total_ms=[0-9]+"
  check "time: the reply was captioned and played" has "$WORK/time" '"state": "sentence_start"'
  check "time: no fallback line" lacks "$WORK/time" "can.t reach my assistant|lost the connection"
  latency "$WORK/time.log"
  said "$WORK/time" 2
fi

if [ "$MODE" = m2 ] && want long; then
  # Voiceless ~5 min task: the session ends after the acknowledgement and the sim device stays offline (no
  # session, no poll) until the task's result card reached the gateway; then it polls and the result is spoken.
  CHAT=$(new_chat)
  check "long: a new chat" test -n "$CHAT"
  T0=$(date +%s)
  SINCE=$(now)
  round long "$LONG" --agent --max-seconds 25
  TASK=$(api GET "/v1/chats/$CHAT" | field open_task_id)
  echo "sim device offline at +$(($(date +%s) - T0)) s, task $TASK"
  check "long: the reply ended with the task still running" \
    sh -c "test -n '$TASK' && grep -qE 'turn_latency .*chat=$CHAT .*reply_end=(hold_elapsed|ack)' '$WORK/long.log'"
  check "long: an acknowledgement was spoken" has "$WORK/long" '"state": "sentence_start", "text": "[^"]+'
  latency "$WORK/long.log"
  said "$WORK/long" 2
  wait_for_log "$SINCE" "agent item queued device=$DEV chat=$CHAT task=$TASK state=(unread|failed|cancelled)" 1200
  T_DONE=$(($(date +%s) - T0))
  api GET "/v1/tasks/$TASK" > "$WORK/long.task"
  "$PY" -c "import json, sys; t = json.load(open(sys.argv[1])); print('task', t.get('state'), t.get('outcome'), 'tier', t.get('result_tier'), 'created', t.get('created_at'), 'finished', t.get('finished_at'), 'cost_usd', round(t.get('cost_usd') or 0, 2))" "$WORK/long.task" | tee -a "$TRANSCRIPT"
  check "long: the task finished done while the device was offline (+$T_DONE s)" has "$WORK/long.task" '"state": *"done"'
  ITEM=$(since_logs "$SINCE" | grep -E "agent item queued device=$DEV chat=$CHAT task=$TASK state=unread" | tail -1)
  echo "$ITEM" | sed 's/^.*agent item/agent item/' | tee -a "$TRANSCRIPT"
  check "long: the result is a spoken WHEN_IDLE item" grep -qE "tier=when_idle speaks=True" <<< "$ITEM"
  DELIVERY=$(grep -oE 'delivery=dlv_[0-9A-Z]+' <<< "$ITEM" | sed 's/delivery=//')
  SPEECH=$(since_logs "$SINCE" | grep -E "agent speech [0-9a-f]+ handed off: delivery=$DELIVERY " | tail -1 \
    | grep -oE 'agent speech [0-9a-f]+' | sed 's/agent speech //')
  check "long: the gateway handed the result to the proactive queue" test -n "$SPEECH"
  T_BACK=$(date +%s)
  BACK=$(now)
  for n in 1 2 3; do
    timeout 120 "$PY" "$SIMDEV" --base $BASE --ws $WS --device $DEV --bind --agent --max-seconds 30 > "$WORK/result-$n" 2>&1
    { echo "## result session $n"; cat "$WORK/result-$n"; } >> "$TRANSCRIPT"
    since_logs "$BACK" | grep -qE "agent speech $SPEECH tts start" && break
  done
  since_logs "$BACK" | grep -E "agent speech|proactive" >> "$TRANSCRIPT"
  check "long: the result was spoken after the device came back (+$(($(date +%s) - T_BACK)) s)" \
    wait_for_log "$BACK" "agent speech $SPEECH tts start" 10
  check "long: the device's played ack reached the gateway" wait_for_log "$BACK" "agent speech $SPEECH played" 10
  cat "$WORK"/result-* | grep -oE '"state": "sentence_start", "text": "[^"]*"' | head -2
fi

if [ "$MODE" = m2 ] && want steer; then
  # Steer a running task, then stop it.
  CHAT=$(new_chat)
  SINCE=$(now)
  round steer "$(printf '%s\n%s\n%s' "$SLOW" "How is it going?" "stop")" --agent --replies 3 --max-seconds 120
  check "steer: the second utterance was routed as a steer" has "$WORK/steer.log" "turn_latency .*chat=$CHAT routed=steer"
  check "steer: \"stop\" was routed as a cancel" has "$WORK/steer.log" "turn_latency .*chat=$CHAT routed=cancel"
  check "steer: \"Stopped.\" was spoken" has "$WORK/steer" '"state": "sentence_start", "text": "Stopped'
  TASK=$(task_of "$CHAT" "$SINCE")
  api GET "/v1/tasks/$TASK" > "$WORK/steer.task"
  check "steer: the task is cancelled ($TASK)" has "$WORK/steer.task" '"state": *"cancelled"'
  latency "$WORK/steer.log"
  said "$WORK/steer" 6
fi

if [ "$MODE" = m2 ] && want barge; then
  # Barge-in at the answer's first audio with 0.6 s of speech: the transcript aborts the turn, which closes its
  # stream.
  CHAT=$(new_chat)
  SINCE=$(now)
  round barge "$(printf '%s\n%s' "Tell me about the history of the Spire in Dublin, in detail." \
    "Sorry, I meant the Ha'penny Bridge. Just one sentence, and don't ask me anything.")" --barge-in 0 --barge-frames 10 --max-seconds 45
  docker logs --since "$SINCE" agent-service 2>&1 | grep -F "\"chat_id\": \"$CHAT\"" \
    | grep -E '"event": "(barge_in|reply_end|accepted)"' > "$WORK/barge.service"
  sed -E 's/"trace_id": "[0-9a-f]*", //' "$WORK/barge.service" >> "$TRANSCRIPT"
  check "barge: the first turn's stream was closed mid-reply" has "$WORK/barge.log" "turn_latency .*chat=$CHAT .*reply_end=closed"
  check "barge: agent-service ended that reply as gateway_closed" has "$WORK/barge.service" '"outcome": "gateway_closed"'
  check "barge: the correction was answered" has "$WORK/barge.log" "turn_latency .*chat=$CHAT .*reply_end=turn_done"
  check "barge: the answer follows the correction" grep -qiE '"text": "[^"]*penny' "$WORK/barge"
  latency "$WORK/barge.log"
  said "$WORK/barge" 4
fi

if [ "$MODE" = m2 ] && want rollback; then
  # VOICE_LLM rollback: the sim server re-rendered with VOICE_LLM=anthropic answers on the legacy path.
  render anthropic
  check "rollback: rendered LLM AnthropicLLM with Intent function_call" \
    sh -c "grep -q '  LLM: AnthropicLLM' data/agent.rendered.yaml && grep -q '  Intent: function_call' data/agent.rendered.yaml"
  SIM_ANTHROPIC_API_KEY=$(env_value ANTHROPIC_API_KEY) "${COMPOSE[@]}" up -d --force-recreate --no-deps sim-server \
    >> "$TRANSCRIPT" 2>&1 || { echo "FAIL compose up (anthropic)"; exit 1; }
  wait_server
  round rollback "What time is it?"
  check "rollback: the turn reached the LLM" has "$WORK/rollback.log" "大模型收到用户消息: What time is it"
  check "rollback: AnthropicLLM answered" has "$WORK/rollback.log" "core.providers.llm.anthropic.anthropic\]-INFO-Token"
  check "rollback: no claude_agent turn" lacks "$WORK/rollback.log" "turn_latency|claude_agent"
  check "rollback: a reply was captioned" has "$WORK/rollback" '"state": "sentence_start", "text": "[^"]+'
  check "rollback: no fallback line" lacks "$WORK/rollback" "can.t reach my assistant|lost the connection"
  said "$WORK/rollback" 2
fi

if [ "$MODE" = m2 ]; then
  render claude_agent
  "${COMPOSE[@]}" down >> "$TRANSCRIPT" 2>&1
fi

if [ "$MODE" = branch ]; then
  round bedside "Set bedside mode." --mcp-tools
  check "bedside: device_action reached the device MCP" has "$WORK/bedside" 'mcp call self.screen.set_bedside .*"mode": "night"'
  check "bedside: the gateway acked the result" has "$WORK/bedside.log" "device_action self.screen.set_bedside ok=True"
  check "bedside: no operator-only tool was called" lacks "$WORK/bedside" 'mcp call self.diag'
  check "bedside: turn_latency line" has "$WORK/bedside.log" "turn_latency .*reply_end="
  latency "$WORK/bedside.log"
  said "$WORK/bedside" 3

  docker stop weixue-agent-sim-service >> "$TRANSCRIPT" 2>&1
  round down "Are you there?"
  check "down: the fallback line is spoken" has "$WORK/down" '"state": "sentence_start", "text": "I can.t reach my assistant right now"'
  check "down: turn_latency reports the fallback" has "$WORK/down.log" "turn_latency .*reply_end=fallback"
  latency "$WORK/down.log"
fi

echo "$PASSES passed, $FAILS failed (transcript: $TRANSCRIPT)"
[ "$FAILS" -eq 0 ]
