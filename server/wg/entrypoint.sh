#!/bin/bash
# Brings up wg0 from the read-only /etc/wireguard/wg0.conf and holds the network namespace.
# Before wg0 exists, traffic to the config's AllowedIPs is allowed out only through wg0 (chain
# WG_BOUNDARY on OUTPUT). DNS lines are dropped so containers in this namespace keep Docker's
# resolver; logs carry no addresses.
set -euo pipefail
SRC=/etc/wireguard/wg0.conf
CONF=/run/wireguard/wg0.conf
KEEPALIVE_S=25
RETRY_S=10
CHAIN=WG_BOUNDARY

redact() {
  sed -E "s/\`[^']*'/\`<redacted>'/g; s/[A-Za-z0-9+\/]{42,43}=/<key>/g; s/[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}:[0-9]+/<endpoint>/g; s/[0-9]+(\.[0-9]+){3}(\/[0-9]+)?/<ip>/g; s/[0-9a-fA-F]{0,4}(:[0-9a-fA-F]{0,4}){2,7}(\/[0-9]+)?/<ip6>/g"
}

# Retries wg-quick up every RETRY_S until it succeeds, logging one redacted line per failure.
up_with_retry() {
  local out
  until out=$(wg-quick up "$CONF" 2>&1); do
    echo "wg: wg-quick up failed, retry in ${RETRY_S} s: $(printf '%s\n' "$out" | awk '!/^\[#\]/ { printf "%s%s", sep, $0; sep = "; " }' | redact)"
    sleep "$RETRY_S" &
    wait $!
  done
}

# AllowedIPs networks of a wg-quick config, one per line.
allowed_ips() {
  sed -nE 's/^[[:space:]]*[Aa][Ll][Ll][Oo][Ww][Ee][Dd][Ii][Pp][Ss][[:space:]]*=(.*)$/\1/p' "$1" | tr ', \t' '\n\n\n' | sed '/^$/d'
}

# Drops every packet to an AllowedIPs network that would leave through an interface other than
# wg0. Refuses a default route in AllowedIPs (split tunnel only) and a config without AllowedIPs.
install_boundary() {
  local nets net v4=0 v6=0
  nets=$(allowed_ips "$1")
  [ -n "$nets" ] || { echo "wg: wg0.conf has no AllowedIPs"; exit 1; }
  for net in $nets; do
    case "$net" in
      0.0.0.0/0 | ::/0) echo "wg: AllowedIPs must not contain a default route"; exit 1 ;;
      *:*) v6=1 ;;
      *) v4=1 ;;
    esac
  done
  for tool in iptables ip6tables; do
    [ "$tool" = iptables ] && [ $v4 = 0 ] && continue
    [ "$tool" = ip6tables ] && [ $v6 = 0 ] && continue
    "$tool" -N "$CHAIN" 2>/dev/null || "$tool" -F "$CHAIN"
    "$tool" -C OUTPUT -j "$CHAIN" 2>/dev/null || "$tool" -I OUTPUT 1 -j "$CHAIN"
  done
  for net in $nets; do
    case "$net" in
      *:*) ip6tables -A "$CHAIN" -d "$net" ! -o wg0 -j DROP ;;
      *) iptables -A "$CHAIN" -d "$net" ! -o wg0 -j DROP ;;
    esac
  done
  echo "wg: boundary: $(printf '%s\n' $nets | wc -l | tr -d ' ') network(s) only through wg0"
}

main() {
  [ -f "$SRC" ] || { echo "wg: $SRC is missing"; exit 1; }
  umask 077
  mkdir -p /run/wireguard
  grep -viE '^[[:space:]]*DNS[[:space:]]*=' "$SRC" > "$CONF"
  install_boundary "$CONF"
  trap 'exit 0' TERM INT
  up_with_retry
  for peer in $(wg show wg0 peers); do
    if [ "$(wg show wg0 persistent-keepalive | awk -v p="$peer" '$1 == p { print $2 }')" = off ]; then
      wg set wg0 peer "$peer" persistent-keepalive "$KEEPALIVE_S"
    fi
  done
  echo "wg: wg0 up, $(wg show wg0 peers | wc -l) peer(s), mtu $(cat /sys/class/net/wg0/mtu)"

  trap 'wg-quick down "$CONF" >/dev/null 2>&1; exit 0' TERM INT
  sleep infinity &
  wait
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main
fi
