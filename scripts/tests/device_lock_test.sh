#!/usr/bin/env bash
# Tests scripts/device_lock.sh against a temporary lock directory.
set -uo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
lib="$root/scripts/device_lock.sh"
tmp="$(mktemp -d)"; export lib tmp
trap 'pkill -f "sleep 9[0-9][0-9][0-9]" 2>/dev/null; rm -rf "$tmp"' EXIT
export WEIXUE_DEVICE_LOCK="$tmp/lock" WEIXUE_DEVICE_LOCK_POLL=1 WEIXUE_DEVICE_LOCK_WAIT=2
unset WEIXUE_DEVICE_LOCK_OWNER
fails=0
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fails=$((fails + 1)); fi; }
user() { bash -c ". '$lib'; device_lock_acquire test >/dev/null || exit 9; $1"; }

user 'test -d "$WEIXUE_DEVICE_LOCK" && grep -q "^pid=$$ .* by=test$" "$WEIXUE_DEVICE_LOCK/owner"'
check "acquire writes pid owner" '[ $? -eq 0 ]'
check "released after normal exit" '[ ! -e "$WEIXUE_DEVICE_LOCK" ]'

user 'exit 3'
check "released after error exit" '[ ! -e "$WEIXUE_DEVICE_LOCK" ]'

bash -c ". '$lib'; device_lock_acquire test >/dev/null; sleep 30 & wait \$!" & victim=$!
sleep 1; kill -TERM "$victim"; wait "$victim" 2>/dev/null
check "released after SIGTERM" '[ ! -e "$WEIXUE_DEVICE_LOCK" ]'

user 'device_lock_cleanup="touch $tmp/cleaned"; true'
check "runs device_lock_cleanup" '[ -f "$tmp/cleaned" ]'

user 'bash -c ". \"$lib\"; device_lock_acquire inner" | grep -q "held by caller" && test -d "$WEIXUE_DEVICE_LOCK"'
check "child of holder reuses lock and leaves it" '[ $? -eq 0 ]'
check "outer release after nested use" '[ ! -e "$WEIXUE_DEVICE_LOCK" ]'

sleep 0 & dead=$!; wait "$dead"
mkdir -p "$WEIXUE_DEVICE_LOCK"; echo "pid=$dead since=x by=gone" > "$WEIXUE_DEVICE_LOCK/owner"
out="$(user 'cat "$WEIXUE_DEVICE_LOCK/owner"' 2>&1)"
check "takes over lock of exited pid" 'echo "$out" | grep -q "taking over stale lock" && echo "$out" | grep -q "by=test"'

sleep 9001 & live=$!
mkdir -p "$WEIXUE_DEVICE_LOCK"; echo "pid=$live since=x by=other" > "$WEIXUE_DEVICE_LOCK/owner"
user 'true' 2>/dev/null; rc=$?
check "waits for live owner, then fails" '[ $rc -eq 9 ] && grep -q "pid=$live" "$WEIXUE_DEVICE_LOCK/owner"'
WEIXUE_DEVICE_LOCK_OWNER="$live" user 'true' >/dev/null 2>&1; rc=$?
check "WEIXUE_DEVICE_LOCK_OWNER grants use" '[ $rc -eq 0 ] && grep -q "pid=$live" "$WEIXUE_DEVICE_LOCK/owner"'
kill "$live"; wait "$live" 2>/dev/null; rm -rf "$WEIXUE_DEVICE_LOCK"

mkdir -p "$WEIXUE_DEVICE_LOCK"; echo "12345 Tue Oct  7 05:00:00 PDT 2026" > "$WEIXUE_DEVICE_LOCK/owner"
start=$SECONDS; out="$(user 'true' 2>&1)"; rc=$?
check "legacy owner: fails at once with hint, lock kept" '[ $rc -eq 9 ] && [ $((SECONDS - start)) -lt 2 ] && echo "$out" | grep -q "WEIXUE_DEVICE_LOCK_OWNER=12345" && grep -q "^12345 " "$WEIXUE_DEVICE_LOCK/owner"'
WEIXUE_DEVICE_LOCK_OWNER=12345 user 'true' >/dev/null 2>&1; rc=$?
check "legacy owner: WEIXUE_DEVICE_LOCK_OWNER grants use, lock kept" '[ $rc -eq 0 ] && grep -q "^12345 " "$WEIXUE_DEVICE_LOCK/owner"'
rm -rf "$WEIXUE_DEVICE_LOCK"
mkdir -p "$WEIXUE_DEVICE_LOCK"
bash -c "echo \"\$\$ \$(date)\" > '$WEIXUE_DEVICE_LOCK/owner'; bash -c \". '$lib'; device_lock_acquire inner\"" | grep -q "held by caller"
check "legacy owner: same-shell holder is the caller" '[ $? -eq 0 ] && [ -d "$WEIXUE_DEVICE_LOCK" ]'
rm -rf "$WEIXUE_DEVICE_LOCK"

out="$("$lib" acquire 9002 2>/dev/null)"; eval "$out"
check "cli acquire prints holder pid" '[ -n "${WEIXUE_DEVICE_LOCK_OWNER:-}" ] && kill -0 "$WEIXUE_DEVICE_LOCK_OWNER"'
check "cli status live" '"$lib" status | grep -q "\[live\]"'
export WEIXUE_DEVICE_LOCK_OWNER
user 'true' >/dev/null 2>&1
check "script under cli holder keeps lock" '[ -d "$WEIXUE_DEVICE_LOCK" ]'
(unset WEIXUE_DEVICE_LOCK_OWNER; "$lib" release 2>/dev/null); rc=$?
check "release refused for non-holder" '[ $rc -ne 0 ] && [ -d "$WEIXUE_DEVICE_LOCK" ]'
holder="$WEIXUE_DEVICE_LOCK_OWNER"
"$lib" release 2>/dev/null
check "release ends holder and removes lock" '[ ! -e "$WEIXUE_DEVICE_LOCK" ] && ! kill -0 "$holder" 2>/dev/null'
unset WEIXUE_DEVICE_LOCK_OWNER

eval "$("$lib" acquire 1 2>/dev/null)"; sleep 2
check "expired holder is stale" '"$lib" status | grep -q "\[stale\]"'
user 'true' 2>/dev/null
check "expired holder lock taken over" '[ $? -eq 0 ] && [ ! -e "$WEIXUE_DEVICE_LOCK" ]'
unset WEIXUE_DEVICE_LOCK_OWNER

live="$(sh -c 'sleep 9003 >/dev/null 2>&1 & echo $!')"
mkdir -p "$WEIXUE_DEVICE_LOCK"; echo "pid=$live since=x by=other" > "$WEIXUE_DEVICE_LOCK/owner"
(sleep 3; kill "$live") &
start=$SECONDS; out="$(WEIXUE_DEVICE_LOCK_WAIT=10 "$lib" acquire 2 2>/dev/null)"; eval "$out"
check "cli acquire after a wait longer than hold is live" '[ $((SECONDS - start)) -ge 3 ] && kill -0 "${WEIXUE_DEVICE_LOCK_OWNER:-0}" && "$lib" status | grep -q "\[live\]"'
sleep 1
check "cli hold counts from acquisition" '"$lib" status | grep -q "\[live\]"'
"$lib" release 2>/dev/null
unset WEIXUE_DEVICE_LOCK_OWNER
wait 2>/dev/null

sleep 0 & dead=$!; wait "$dead"
mkdir -p "$WEIXUE_DEVICE_LOCK"; echo "pid=$dead since=x by=gone" > "$WEIXUE_DEVICE_LOCK/owner"
for i in 1 2 3 4 5 6; do
  WEIXUE_DEVICE_LOCK_WAIT=30 user 'grep -q "^pid=$$ " "$WEIXUE_DEVICE_LOCK/owner" && sleep 0.3 && grep -q "^pid=$$ " "$WEIXUE_DEVICE_LOCK/owner" && mkdir "$tmp/inside" && sleep 0.2 && rmdir "$tmp/inside"' >/dev/null 2>&1 \
    && echo ok >> "$tmp/race" || echo "fail $?" >> "$tmp/race" &
done
wait
check "concurrent takeover of a stale lock: each waiter holds it alone" '[ "$(grep -c "^ok$" "$tmp/race")" -eq 6 ] && [ ! -e "$WEIXUE_DEVICE_LOCK" ] && [ -z "$(ls -d "$WEIXUE_DEVICE_LOCK".stale* 2>/dev/null)" ]'

echo "failures: $fails"
[ "$fails" -eq 0 ]
