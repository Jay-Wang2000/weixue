#!/usr/bin/env bash
# Device lock for the connected board: mkdir of ${WEIXUE_DEVICE_LOCK:-$HOME/Projects/weixue-vendor/.device-lock}, owner file "pid=<pid> since=<UTC> by=<label>".
# A lock whose pid has exited is taken over and a live one is waited for; an owner file "<pid> <date>" is never taken over and fails at once unless the caller holds it.
# The caller holds the lock when the owner pid (live, for the pid= form) is this process, an ancestor of it, or $WEIXUE_DEVICE_LOCK_OWNER.
# Sourced: device_lock_acquire <label> takes the lock (or uses the caller's) and releases a lock it took on exit, after running $device_lock_cleanup.
# Run: device_lock.sh acquire [seconds] | release | status; acquire takes the lock, then keeps it with a holder process for [seconds] (default 1500) from then, and prints "export WEIXUE_DEVICE_LOCK_OWNER=<pid>".

_device_lock_dir="${WEIXUE_DEVICE_LOCK:-${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}/.device-lock}"
_device_lock_poll="${WEIXUE_DEVICE_LOCK_POLL:-15}"

_device_lock_owner_pid() {
  sed -n 's/^pid=\([0-9][0-9]*\) .*/\1/p' "$_device_lock_dir/owner" 2>/dev/null | head -1
}

_device_lock_any_pid() {
  sed -n -e 's/^pid=\([0-9][0-9]*\) .*/\1/p' -e 's/^\([0-9][0-9]*\) .*/\1/p' "$_device_lock_dir/owner" 2>/dev/null | head -1
}

_device_lock_alive() {
  kill -0 "$1" 2>/dev/null || ps -p "$1" >/dev/null 2>&1
}

_device_lock_held_by_caller() {
  local owner p
  owner="$(_device_lock_any_pid)"
  [ -n "$owner" ] || return 1
  [ -z "$(_device_lock_owner_pid)" ] || _device_lock_alive "$owner" || return 1
  [ "$owner" = "${WEIXUE_DEVICE_LOCK_OWNER:-}" ] && return 0
  p=$$
  while [ -n "$p" ] && [ "$p" -gt 1 ]; do
    [ "$p" = "$owner" ] && return 0
    p="$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')"
  done
  return 1
}

# Removes the lock when its owner file still reads $1 and that pid has exited, under an flock on $dir.takeover.
_device_lock_take_stale() {
  python3 - "$_device_lock_dir" "$1" <<'PY'
import fcntl, os, re, shutil, sys
lock, seen = sys.argv[1], sys.argv[2]
with open(lock + ".takeover", "a") as guard:
    fcntl.flock(guard, fcntl.LOCK_EX)
    try:
        with open(os.path.join(lock, "owner")) as f:
            current = f.read().rstrip("\n")
    except OSError:
        sys.exit(0)
    m = re.match(r"pid=(\d+) ", current)
    if current != seen or not m:
        sys.exit(0)
    try:
        os.kill(int(m.group(1)), 0)
        sys.exit(0)
    except ProcessLookupError:
        pass
    except PermissionError:
        sys.exit(0)
    shutil.rmtree(lock)
PY
}

_device_lock_write_owner() {
  printf 'pid=%s since=%s by=%s\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$2" > "$_device_lock_dir/owner.$$" \
    && mv -f "$_device_lock_dir/owner.$$" "$_device_lock_dir/owner"
}

# Takes the lock with this process as owner; waits up to ${WEIXUE_DEVICE_LOCK_WAIT:-2400} s.
_device_lock_take() {
  local label="$1" waited=0 seen owner told=""
  mkdir -p "$(dirname "$_device_lock_dir")"
  while ! mkdir "$_device_lock_dir" 2>/dev/null; do
    seen="$(cat "$_device_lock_dir/owner" 2>/dev/null || true)"
    owner="$(_device_lock_owner_pid)"
    if [ -n "$owner" ] && ! _device_lock_alive "$owner"; then
      echo "device lock: taking over stale lock ($seen)" >&2
      _device_lock_take_stale "$seen" || { echo "error: device lock takeover failed" >&2; return 1; }
      continue
    fi
    if [ -z "$owner" ] && [ -n "$seen" ]; then
      echo "error: device lock $_device_lock_dir is held by '$seen'; if this caller holds it, rerun with WEIXUE_DEVICE_LOCK_OWNER=$(_device_lock_any_pid)" >&2
      return 1
    fi
    if [ "$waited" -ge "${WEIXUE_DEVICE_LOCK_WAIT:-2400}" ]; then
      echo "error: device lock $_device_lock_dir still held after ${waited} s (${seen:-no owner file})" >&2
      return 1
    fi
    [ "$told" = "$seen" ] || { echo "device lock: waiting for ${seen:-an owner file}" >&2; told="$seen"; }
    sleep "$_device_lock_poll"
    waited=$((waited + _device_lock_poll))
  done
  _device_lock_write_owner $$ "$label"
}

_device_lock_release_mine() {
  [ "$(_device_lock_owner_pid)" = "$1" ] && rm -rf "$_device_lock_dir"
  return 0
}

_device_lock_mine=""
_device_lock_exit() {
  [ -z "${device_lock_cleanup:-}" ] || eval "$device_lock_cleanup" || true
  [ -z "$_device_lock_mine" ] || _device_lock_release_mine "$_device_lock_mine"
}

device_lock_acquire() {
  trap _device_lock_exit EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  if _device_lock_held_by_caller; then
    echo "device lock: held by caller ($(cat "$_device_lock_dir/owner"))"
    return 0
  fi
  _device_lock_take "$1" || return 1
  _device_lock_mine=$$
  echo "device lock: acquired ($(cat "$_device_lock_dir/owner"))"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  case "${1:-}" in
    acquire)
      hold="${2:-1500}"
      case "$hold" in ''|*[!0-9]*) echo "error: hold seconds must be a number" >&2; exit 2 ;; esac
      _device_lock_take "device_lock.sh acquire" || exit 1
      nohup sleep "$hold" >/dev/null 2>&1 &
      holder=$!
      disown "$holder" 2>/dev/null || true
      _device_lock_write_owner "$holder" "holder(${hold}s)" || { kill "$holder" 2>/dev/null; rm -rf "$_device_lock_dir"; exit 1; }
      echo "device lock: acquired ($(cat "$_device_lock_dir/owner"))" >&2
      echo "export WEIXUE_DEVICE_LOCK_OWNER=$holder"
      ;;
    release)
      owner="$(_device_lock_owner_pid)"
      [ -n "$owner" ] || { echo "no device lock with a pid owner at $_device_lock_dir" >&2; exit 1; }
      _device_lock_held_by_caller || { echo "error: device lock is held by pid $owner, not by the caller" >&2; exit 1; }
      [ "$owner" = "$$" ] || kill "$owner" 2>/dev/null || true
      rm -rf "$_device_lock_dir"
      echo "device lock: released (pid $owner)" >&2
      ;;
    status)
      if [ -d "$_device_lock_dir" ]; then
        owner="$(_device_lock_owner_pid)"
        state=unknown-format
        [ -z "$owner" ] || { _device_lock_alive "$owner" && state=live || state=stale; }
        echo "held: $(cat "$_device_lock_dir/owner" 2>/dev/null || echo 'no owner file') [$state]"
      else
        echo "free"
      fi
      ;;
    *) echo "usage: $0 acquire [seconds] | release | status" >&2; exit 2 ;;
  esac
fi
