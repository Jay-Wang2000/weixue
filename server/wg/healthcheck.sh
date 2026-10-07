#!/bin/sh
# Healthy while wg0 has a handshake younger than 180 s (WireGuard's session key lifetime).
last=$(wg show wg0 latest-handshakes 2>/dev/null | awk 'BEGIN { m = 0 } $2 > m { m = $2 } END { print m }')
[ "${last:-0}" -gt 0 ] && [ $(( $(date +%s) - last )) -lt 180 ]
