#!/usr/bin/env bash
# docker on the host that runs the stack: this one, or over SSH when WEIXUE_SERVER_SSH is user@host.
# WEIXUE_SERVER_SSH, WEIXUE_SERVER_SSH_OPTS (extra ssh options, split on spaces) and WEIXUE_SERVER_DIR (the server/
# checkout there, default weixue/server under the SSH user's home) come from the environment when set there, an
# empty value included, else from the live server/.env (WEIXUE_LIVE_ENV, default ~/Projects/weixue/server/.env).
# Local compose runs on the live .env's directory. scripts/lib/remote_docker.py is the Python equivalent.
#
#   . scripts/lib/remote_docker.sh; weixue_docker logs --since 60s agent-service; weixue_compose ps
#   scripts/lib/remote_docker.sh logs agent-service | compose ps | host

weixue_live_env() { printf '%s\n' "${WEIXUE_LIVE_ENV:-$HOME/Projects/weixue/server/.env}"; }

# KEY from the environment when set there, else its last assignment in the live .env without surrounding quotes.
weixue_setting() {
  if [ -n "${!1+x}" ]; then
    printf '%s\n' "${!1}"
  else
    sed -n "s/^$1=//p" "$(weixue_live_env)" 2>/dev/null | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
  fi
}

# Host part of WEIXUE_SERVER_SSH; empty in local mode.
weixue_server_host() {
  local target
  target=$(weixue_setting WEIXUE_SERVER_SSH)
  [ -z "$target" ] || printf '%s\n' "${target#*@}"
}

# One POSIX single-quoted word per argument, space-separated, for the remote shell.
weixue_quote() {
  local arg rest out="" q="'"
  for arg in "$@"; do
    out+=" '"
    rest=$arg
    while [[ $rest == *$q* ]]; do
      out+="${rest%%$q*}'\\''"
      rest=${rest#*$q}
    done
    out+="$rest'"
  done
  printf '%s' "${out# }"
}

weixue_ssh() {
  local target opts=()
  target=$(weixue_setting WEIXUE_SERVER_SSH)
  read -r -a opts <<< "$(weixue_setting WEIXUE_SERVER_SSH_OPTS)"
  ssh -o BatchMode=yes -o ConnectTimeout=10 ${opts[@]+"${opts[@]}"} "$target" "$1"
}

weixue_docker() {
  if [ -z "$(weixue_setting WEIXUE_SERVER_SSH)" ]; then
    docker "$@"
  else
    weixue_ssh "docker $(weixue_quote "$@")"
  fi
}

weixue_compose() {
  local dir
  if [ -z "$(weixue_setting WEIXUE_SERVER_SSH)" ]; then
    docker compose -f "$(dirname "$(weixue_live_env)")/docker-compose.yml" "$@"
  else
    dir=$(weixue_setting WEIXUE_SERVER_DIR)
    weixue_ssh "cd $(weixue_quote "${dir:-weixue/server}") && docker compose $(weixue_quote "$@")"
  fi
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -euo pipefail
  case "${1:-}" in
    "") echo "usage: $0 <docker args> | compose <compose args> | host" >&2; exit 2 ;;
    compose) shift; weixue_compose "$@" ;;
    host) weixue_server_host ;;
    *) weixue_docker "$@" ;;
  esac
fi
