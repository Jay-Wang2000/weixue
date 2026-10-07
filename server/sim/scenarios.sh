#!/bin/bash
# Pass/fail gate for the server/sim stack (docker-compose.yml here): every scenario asserts its
# HTTP statuses, terminal outcomes, attempts and server log lines, with idempotency keys unique to
# the run. Raw sim-device and server output (the transcript) goes to TRANSCRIPT (default: a file
# under sim/, gitignored); the gate prints one PASS/FAIL line per check and exits 1 on any FAIL.
#   cd server/sim && docker compose -p weixue-wave3-sim up -d && ./scenarios.sh [TRANSCRIPT]
# Needs the repo's .venv (server/requirements-test.txt).
cd "$(dirname "$0")"
export PATH="$HOME/.rd/bin:$PATH"
ROOT=$(cd ../.. && pwd)
PY="$ROOT/.venv/bin/python"
export DYLD_FALLBACK_LIBRARY_PATH="$ROOT/.venv/lib"
SIMDEV="$ROOT/server/scripts/proactive_sim_device.py"
CONTAINER=weixue-wave3-sim-server
BASE=http://127.0.0.1:18103
WS=ws://127.0.0.1:18100/xiaozhi/v1/
AUTH="Authorization: Bearer scratch-sim-token"
DEV=aa:bb:cc:dd:ee:01
REPLY="$ROOT/server/tests/assets/en-US_turn_on_the_living_room_lights.wav"
RUN="run-$(date +%s)-$$"
TRANSCRIPT=${1:-$PWD/scenarios-$RUN.out}
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
PASSES=0
FAILS=0
: > "$TRANSCRIPT"

note() { printf '%s\n' "$*" >> "$TRANSCRIPT"; }
check() {  # check DESCRIPTION COMMAND...
  local what=$1
  shift
  if "$@"; then PASSES=$((PASSES + 1)); echo "PASS $what"; else FAILS=$((FAILS + 1)); echo "FAIL $what"; fi
}
has() { grep -qF -- "$2" "$1"; }
lacks() { ! grep -qF -- "$2" "$1"; }
count_is() { [ "$(grep -cF -- "$2" "$1")" -eq "$3" ]; }
post() {
  local out
  out=$(curl -s -X POST $BASE/weixue/proactive/messages -H "$AUTH" -H "Content-Type: application/json" -d "$1")
  note "POST $1 -> $out"
  printf '%s' "$out"
}
idof() { python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])'; }
field() { python3 -c "import json,sys;print(json.load(sys.stdin).get('$1'))"; }
record() { curl -s -H "$AUTH" $BASE/weixue/proactive/messages/$1; }
final() {  # final ID [TIMEOUT]: the record once its status is terminal, else the last one seen
  local end=$((SECONDS + ${2:-30})) body status
  while :; do
    body=$(record "$1")
    status=$(printf '%s' "$body" | field status)
    case "$status" in delivered|answered|no_reply|partial|expired|failed) break ;; esac
    [ $SECONDS -lt $end ] || break
    sleep 0.5
  done
  note "record $1: $body"
  printf '%s' "$body"
}
is() { [ "$(printf '%s' "$1" | field "$2")" = "$3" ]; }
sim() {  # sim NAME ARGS...: runs the sim device, output in $WORK/NAME
  local name=$1
  shift
  "$PY" "$SIMDEV" --base $BASE --ws $WS "$@" > "$WORK/$name" 2>&1
  { echo "-- sim $name: $*"; cat "$WORK/$name"; } >> "$TRANSCRIPT"
}
since() { date -u +%Y-%m-%dT%H:%M:%SZ; }
serverlog() {  # serverlog SINCE NAME: server log since SINCE in $WORK/NAME
  sleep 0.5
  docker logs --since "$1" $CONTAINER > "$WORK/$2" 2>&1
  { echo "-- server log $2"; grep -E 'weixue_|识别文本|Abort message received$' "$WORK/$2"; } >> "$TRANSCRIPT"
}
transcripts() { printf '%s\n' "$@" > data/sim_transcripts.txt; }

docker inspect -f '{{.State.Running}}' $CONTAINER 2>/dev/null | grep -q true \
  || { echo "FAIL $CONTAINER is not running (docker compose -p weixue-wave3-sim up -d)"; exit 1; }

note "## 1. poll identity"
for case in "aa:bb:cc:dd:ee:99|unlisted" "|missing" "AA:BB:CC:DD:EE:01|upper-case"; do
  id=${case%%|*}
  code=$(curl -s -o /dev/null -w '%{http_code}' ${id:+-H "Device-Id: $id"} $BASE/weixue/proactive/poll)
  note "poll Device-Id ${case#*|}: $code"
  check "1 poll with a ${case#*|} Device-Id is 401" [ "$code" = 401 ]
done

note "## 2. WebSocket from a device not on allowed_devices"
T=$(since)
sim ws-unlisted --device aa:bb:cc:dd:ee:99 --direct --max-seconds 5
serverlog "$T" ws-unlisted.log
check "2 unlisted WebSocket gets no session" lacks "$WORK/ws-unlisted" "hello "
check "2 server refuses the unlisted device" has "$WORK/ws-unlisted.log" "WebSocket refused: device 'aa:bb:cc:dd:ee:99' is not in allowed_devices"

note "## 3. announce from idle; the poll answer carries id, urgent and lease"
ID=$(post '{"mode":"announce","message":"The washing machine has finished.","id":"'$RUN'-announce"}' | idof)
sim announce --device $DEV --max-seconds 20
REC=$(final "$ID")
check "3 poll answer names the message and a lease" grep -qE "poll 200 \{\"pending\": true, \"urgent\": false, \"id\": \"$ID\", \"lease\": \"[0-9a-f]{32}\"\}" "$WORK/announce"
check "3 announce delivered" is "$REC" status delivered
check "3 on attempt 1" is "$REC" attempts 1

note "## 4. invoke with no lease from an allowed device"
ID=$(post '{"mode":"announce","message":"For another device.","id":"'$RUN'-noreserve","device":"aa:bb:cc:dd:ee:02","ttl":30}' | idof)
T=$(since)
sim noreserve --device $DEV --direct --invoke --max-seconds 5
serverlog "$T" noreserve.log
check "4 session closed by the server" has "$WORK/noreserve" "session ended: server closed"
check "4 server logs no current lease" has "$WORK/noreserve.log" "proactive session opened with no current lease for it; closing"
check "4 message for the other device stays queued" is "$(record "$ID")" status queued

note "## 5. synthesis failure before any audio: requeued twice, then failed"
ID=$(post '{"mode":"announce","message":"SIMFAIL Water detected under the sink.","id":"'$RUN'-fail","ttl":60}' | idof)
T=$(since)
for i in 1 2 3; do sim fail-$i --device $DEV --polls 3 --max-seconds 10; done
REC=$(final "$ID" 20)
serverlog "$T" fail.log
check "5 failed" is "$REC" status failed
check "5 after 3 attempts" is "$REC" attempts 3
check "5 error is no_audio" is "$REC" error no_audio
check "5 requeued twice" count_is "$WORK/fail.log" "requeued: no_audio" 2

note "## 6. urgent conversation, a real Yes after the opener"
transcripts "Yes."
ID=$(post '{"mode":"conversation","message":"Water detected under the kitchen sink. Do you want me to turn off the main water valve?","id":"'$RUN'-conv-yes","urgent":true}' | idof)
sim conv-yes --device $DEV --reply "$REPLY" --reply-delay 0.5 --max-seconds 25
REC=$(final "$ID")
check "6 answered" is "$REC" status answered
check "6 reply is Yes." is "$REC" reply "Yes."
check "6 poll answer is urgent" has "$WORK/conv-yes" '"urgent": true'

note "## 7. echo guard: the playing sentence's echo is dropped, then a real Yes"
transcripts "water detected under the kitchen sink" "Yes."
ID=$(post '{"mode":"conversation","message":"Water detected under the kitchen sink. Do you want me to turn off the main water valve?","id":"'$RUN'-conv-echo"}' | idof)
T=$(since)
sim conv-echo --device $DEV --reply "$REPLY" --reply-during-playback --reply-delay 0.3 --max-seconds 30
REC=$(final "$ID")
serverlog "$T" conv-echo.log
check "7 echo ignored" has "$WORK/conv-echo.log" "playback echo ignored: 'water detected under the kitchen sink'"
check "7 no barge-in" lacks "$WORK/conv-echo.log" "Abort message received"
check "7 answered with Yes." is "$REC" reply "Yes."

note "## 8. real speech during playback barges in"
transcripts "Stop." "Yes."
ID=$(post '{"mode":"conversation","message":"Water detected under the kitchen sink. Do you want me to turn off the main water valve?","id":"'$RUN'-conv-bargein"}' | idof)
T=$(since)
sim conv-bargein --device $DEV --reply "$REPLY" --reply-during-playback --reply-delay 0.3 --max-seconds 30
REC=$(final "$ID")
serverlog "$T" conv-bargein.log
check "8 Stop. barges in" has "$WORK/conv-bargein.log" "Abort message received"
check "8 Stop. is not taken for echo" lacks "$WORK/conv-bargein.log" "playback echo ignored"
check "8 answered" is "$REC" status answered

note "## 9. a correction that reverses the opener barges in"
for case in "Do not turn on the living room light.|negation" "Turn off the living room light.|opposite"; do
  said=${case%%|*}
  kind=${case#*|}
  transcripts "$said" "Yes."
  ID=$(post '{"mode":"conversation","message":"I will turn on the living room light now. Is that okay with you?","id":"'$RUN'-correct-'$kind'"}' | idof)
  T=$(since)
  sim correct-$kind --device $DEV --reply "$REPLY" --reply-during-playback --reply-delay 0.3 --max-seconds 30
  REC=$(final "$ID")
  serverlog "$T" correct-$kind.log
  check "9 $kind correction is not taken for echo" lacks "$WORK/correct-$kind.log" "playback echo ignored"
  check "9 $kind correction barges in" has "$WORK/correct-$kind.log" "Abort message received"
  check "9 $kind correction is the reply" is "$REC" reply "$said"
done

note "## 10. a second client polls with the device id while the device's session is open"
"$PY" "$SIMDEV" --base $BASE --ws $WS --device $DEV --direct --max-seconds 15 > "$WORK/busy-direct" 2>&1 &
sleep 1.5
"$PY" "$SIMDEV" --base $BASE --ws $WS --device $DEV --polls 1 --max-seconds 5 > "$WORK/busy-second" 2>&1 &
sleep 0.5
ID=$(post '{"mode":"announce","message":"The dryer has finished.","id":"'$RUN'-busy"}' | idof)
wait
{ echo "-- device session"; cat "$WORK/busy-direct"; echo "-- second client"; cat "$WORK/busy-second"; } >> "$TRANSCRIPT"
REC=$(final "$ID")
check "10 message played in the device's session" has "$WORK/busy-direct" '"text": "The dryer has finished"'
check "10 second client's poll is 204" has "$WORK/busy-second" "poll 204"
check "10 delivered" is "$REC" status delivered

note "## 11. every wake cue, each followed by a real Yes that reaches the LLM"
transcripts "Yes."
SEEN=" "
for i in $(seq 1 30); do
  sim wake-$i --device $DEV --direct --wake Jarvis --reply "$REPLY" --reply-delay 0.3 --max-seconds 8
  cue=$(grep -oE '"text": "(Hmm|Mm-hm|Yes)\?"' "$WORK/wake-$i" | head -1 | sed -E 's/.*"([^"]+)"$/\1/')
  if [ -n "$cue" ] && has "$WORK/wake-$i" '"type": "stt", "text": "Yes"' && has "$WORK/wake-$i" '"text": "I will close the valve"'; then
    case "$SEEN" in *" $cue "*) ;; *) SEEN="$SEEN$cue " ;; esac
  fi
  [ "$(echo $SEEN | wc -w | tr -d ' ')" -lt 3 ] || break
done
note "cues followed by Yes and the LLM reply:$SEEN"
for cue in "Hmm?" "Mm-hm?" "Yes?"; do
  check "11 cue $cue then Yes reaches the LLM" has <(printf '%s' "$SEEN") " $cue "
done

note "## 12. abort right after the wake drops the cue"
T=$(since)
sim wake-abort --device $DEV --direct --wake Jarvis --abort-after 0.2 --max-seconds 5
serverlog "$T" wake-abort.log
check "12 cue skipped after the abort" has "$WORK/wake-abort.log" "wake cue skipped: user speaking, abort or newer turn"
check "12 no cue caption sent" sh -c "! grep -qE '\"text\": \"(Hmm|Mm-hm|Yes)[?]\"' '$WORK/wake-abort'"

note "## 13. a lease that expired before the session opened consumes nothing"
ID=$(post '{"mode":"announce","message":"The oven is preheated.","id":"'$RUN'-stale"}' | idof)
T=$(since)
sim stale --device $DEV --polls 1 --invoke-delay 16 --max-seconds 5
serverlog "$T" stale.log
REC=$(record "$ID")
note "record $ID: $REC"
check "13 stale session closed" has "$WORK/stale.log" "proactive session opened with no current lease for it; closing"
check "13 message still queued" is "$REC" status queued
check "13 not attempted" is "$REC" attempts 0
sim stale-retry --device $DEV --bind --max-seconds 15
REC=$(final "$ID")
check "13 the next poll's bound invocation delivers it" is "$REC" status delivered
check "13 on attempt 1" is "$REC" attempts 1

note "## 14. synthesis fails after its first audio chunk"
ID=$(post '{"mode":"announce","message":"SIMBREAK The dryer has finished.","id":"'$RUN'-break"}' | idof)
sim break --device $DEV --max-seconds 15
REC=$(final "$ID")
check "14 partial" is "$REC" status partial
check "14 error names the synthesis" is "$REC" error "synthesis failed after audio started"
check "14 not retried" is "$REC" attempts 1

echo "$PASSES passed, $FAILS failed (transcript: $TRANSCRIPT)"
[ "$FAILS" -eq 0 ]
