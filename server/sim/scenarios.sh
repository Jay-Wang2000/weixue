#!/bin/bash
# Proactive-delivery checks against the server/sim stack (docker-compose.yml here), driven by
# server/scripts/proactive_sim_device.py with the repo's .venv (requirements-test.txt).
cd "$(dirname "$0")"
ROOT=$(cd ../.. && pwd)
PY="$ROOT/.venv/bin/python"
export DYLD_FALLBACK_LIBRARY_PATH="$ROOT/.venv/lib"
SIMDEV="$ROOT/server/scripts/proactive_sim_device.py"
BASE=http://127.0.0.1:18103
WS=ws://127.0.0.1:18100/xiaozhi/v1/
AUTH="Authorization: Bearer scratch-sim-token"
DEV=aa:bb:cc:dd:ee:01
REPLY="$ROOT/server/tests/assets/en-US_turn_on_the_living_room_lights.wav"
post() { curl -s -X POST $BASE/weixue/proactive/messages -H "$AUTH" -H "Content-Type: application/json" -d "$1"; echo; }
status() { curl -s -H "$AUTH" $BASE/weixue/proactive/messages/$1; echo; }
sim() { "$PY" "$SIMDEV" --base $BASE --ws $WS "$@"; }
idof() { python3 -c 'import json,sys;print(json.load(sys.stdin)["id"])'; }
echo "## 1. S1 poll identity"
curl -s -o /dev/null -w "poll, Device-Id not on allowed_devices: %{http_code}\n" -H "Device-Id: aa:bb:cc:dd:ee:99" $BASE/weixue/proactive/poll
curl -s -o /dev/null -w "poll, no Device-Id: %{http_code}\n" $BASE/weixue/proactive/poll
curl -s -o /dev/null -w "poll, upper-case form of a listed Device-Id: %{http_code}\n" -H "Device-Id: AA:BB:CC:DD:EE:01" $BASE/weixue/proactive/poll
echo "## 2. S1 WebSocket from a device not on allowed_devices"
sim --device aa:bb:cc:dd:ee:99 --direct --max-seconds 5
echo "## 3. Announce from idle (poll answer carries id + urgent)"
ID=$(post '{"mode":"announce","message":"The washing machine has finished.","id":"run-announce"}' | idof)
sim --device $DEV --max-seconds 20; status $ID
echo "## 4. S1 invoke from an allowed device with no reservation"
post '{"mode":"announce","message":"For another device.","id":"run-noreserve","device":"aa:bb:cc:dd:ee:02"}' >/dev/null
sim --device $DEV --direct --invoke --max-seconds 5
echo "## 5. C7 synthesis failure (fake client raises InvalidArgument)"
ID=$(post '{"mode":"announce","message":"SIMFAIL Water detected under the sink.","id":"run-fail","ttl":60}' | idof)
for i in 1 2 3; do sim --device $DEV --polls 1 --max-seconds 10; done; status $ID
echo "## 6. Conversation, urgent, real Yes after the opener"
printf "Yes.\n" > data/sim_transcripts.txt
ID=$(post '{"mode":"conversation","message":"Water detected under the kitchen sink. Do you want me to turn off the main water valve?","id":"run-conv-yes","urgent":true}' | idof)
sim --device $DEV --reply $REPLY --reply-delay 0.5 --max-seconds 25; status $ID
echo "## 7. Echo guard: utterance during the opener transcribed as the opener, then a real Yes"
printf "Do you want me to turn off the main water valve\nYes.\n" > data/sim_transcripts.txt
ID=$(post '{"mode":"conversation","message":"Water detected under the kitchen sink. Do you want me to turn off the main water valve?","id":"run-conv-echo"}' | idof)
sim --device $DEV --reply $REPLY --reply-during-playback --reply-delay 0.3 --max-seconds 30; status $ID
echo "## 8. Real speech during playback still barges in (transcript Stop.)"
printf "Stop.\nYes.\n" > data/sim_transcripts.txt
ID=$(post '{"mode":"conversation","message":"Water detected under the kitchen sink. Do you want me to turn off the main water valve?","id":"run-conv-bargein"}' | idof)
sim --device $DEV --reply $REPLY --reply-during-playback --reply-delay 0.3 --max-seconds 30; status $ID
echo "## 9. S1 second client polls with the device id while the device's session is open"
sim --device $DEV --direct --max-seconds 15 > direct.out 2>&1 &
sleep 1.5
sim --device $DEV --polls 1 --max-seconds 5 > second.out 2>&1 &
sleep 0.5
ID=$(post '{"mode":"announce","message":"The dryer has finished.","id":"run-busy"}' | idof)
wait
echo "-- device session:"; cat direct.out; echo "-- second client:"; cat second.out; status $ID
echo "## 10. Wake cue then a real Yes (every template cue; loops until Yes? has played)"
printf "Yes.\n" > data/sim_transcripts.txt
for i in $(seq 1 12); do
  sim --device $DEV --direct --wake Jarvis --reply $REPLY --reply-delay 0.3 --max-seconds 8 > wake.out 2>&1
  CUE=$(grep -o '"text": "[^"]*?"' wake.out | head -1)
  echo "-- run $i: cue $CUE"; grep -E '"stt"|sentence_start' wake.out
  case "$CUE" in *Yes?*) break;; esac
done
echo "## 11. C7 synthesis fails after its first audio chunk"
ID=$(post '{"mode":"announce","message":"SIMBREAK The dryer has finished.","id":"run-break"}' | idof)
sim --device $DEV --max-seconds 15; status $ID
