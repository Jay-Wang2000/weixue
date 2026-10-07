#!/usr/bin/env bash
# Idempotent bootstrap: model download, config render, build, start stack.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.rd/bin:$PATH"

[ -f .env ] || { echo "missing server/.env (copy .env.example)"; exit 1; }
set -a; . ./.env; set +a
: "${LAN_IP:?}" "${TZ_OFFSET:?}" "${ANTHROPIC_API_KEY:?}" "${GOOGLE_SA_FILE:?}" "${HA_MCP_URL:?}"
[ -f "$GOOGLE_SA_FILE" ] || { echo "GOOGLE_SA_FILE does not exist"; exit 1; }

# WG_TUNNEL=1 runs mcp-bridge in the wg sidecar's network namespace (docs/server.md, Office / off-LAN mode).
# wg0.conf is checked by existence and mode only; its contents are never read here.
WG_CONF=.secrets/wg0.conf
case "${WG_TUNNEL:-}" in
  "") TUNNEL_SERVICES="" ;;
  1)
    [ -f "$WG_CONF" ] || { echo "WG_TUNNEL=1 but server/$WG_CONF does not exist"; exit 1; }
    [ -n "$(find "$WG_CONF" -maxdepth 0 -perm 0600)" ] || { echo "server/$WG_CONF must be mode 0600"; exit 1; }
    TUNNEL_SERVICES=wg
    ;;
  *) echo "WG_TUNNEL must be 1 or empty"; exit 1 ;;
esac

# COMPOSE_PROFILES in .env holds `tunnel` exactly while WG_TUNNEL=1, so every docker compose command in server/ sees wg.
ENV_PROFILES=$(sed -n 's/^COMPOSE_PROFILES=//p' .env | tail -n 1 | sed -E "s/^\"(.*)\"$/\\1/; s/^'(.*)'$/\\1/")
PROFILES=$(printf '%s' "$ENV_PROFILES" | tr ',' '\n' | awk '$0 != "" && $0 != "tunnel" { printf "%s%s", sep, $0; sep = "," }')
[ -z "$TUNNEL_SERVICES" ] || PROFILES=${PROFILES:+$PROFILES,}tunnel
if [ "$PROFILES" != "$ENV_PROFILES" ]; then
  ENV_REST=$(grep -v '^COMPOSE_PROFILES=' .env || true)
  { printf '%s\n' "$ENV_REST"; [ -z "$PROFILES" ] || echo "COMPOSE_PROFILES=$PROFILES"; } > .env
fi
if [ -n "$PROFILES" ]; then export COMPOSE_PROFILES=$PROFILES; else unset COMPOSE_PROFILES; fi

MODEL=data/models/SenseVoiceSmall/model.pt
MODEL_URL=https://modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt
mkdir -p data/models/SenseVoiceSmall data/mcp-endpoint data/mcp-bridge
if [ -d "$MODEL" ]; then
  rmdir "$MODEL" 2>/dev/null || { echo "$MODEL is a non-empty directory; remove it"; exit 1; }
fi
if [ ! -f "$MODEL" ] || [ "$(wc -c < "$MODEL")" -lt 900000000 ]; then
  curl -L --fail -C - -o "$MODEL" "$MODEL_URL"
fi

VENDOR="${WEIXUE_VENDOR:-$HOME/Projects/weixue-vendor}/xiaozhi-esp32-server"
UPSTREAM_COMMIT=$(awk '$1=="xiaozhi-esp32-server"{print $3}' ../vendor.lock)
[ "$(git -C "$VENDOR" rev-parse HEAD)" = "$UPSTREAM_COMMIT" ] \
  || { echo "$VENDOR is not at the commit pinned in vendor.lock"; exit 1; }
[ -z "$(git -C "$VENDOR" status --porcelain)" ] \
  || { echo "$VENDOR has local changes; overlay them in server/overlay/"; exit 1; }
export UPSTREAM_COMMIT

docker info >/dev/null 2>&1 || { echo "docker daemon is not running"; exit 1; }

docker compose up -d mcp-endpoint-server
MCP=""
for _ in $(seq 1 30); do
  MCP=$(docker logs mcp-endpoint-server 2>&1 | sed $'s/\x1b\\[[0-9;]*m//g' | grep -o 'ws://[^ ]*/mcp_endpoint/mcp/?token=[^ ]*' | tail -1 || true)
  [ -n "$MCP" ] && break; sleep 1
done
[ -n "$MCP" ] || { echo "mcp endpoint URL not found in logs"; exit 1; }
chmod 600 data/mcp-endpoint/.mcp-endpoint-server.cfg
MCP_ENDPOINT_INTERNAL=$(echo "$MCP" | sed -E 's#^ws://[^/]+#ws://mcp-endpoint-server:8004#')
(umask 077; printf '%s\n' "$MCP_ENDPOINT_INTERNAL" > data/mcp-bridge/endpoint-url)
chmod 600 data/mcp-bridge/endpoint-url

MCP_ENDPOINT=$MCP_ENDPOINT_INTERNAL \
  python3 scripts/render_config.py config/config.template.yaml > data/.config.yaml
chmod 600 data/.config.yaml

docker compose up -d --build --force-recreate $TUNNEL_SERVICES mcp-bridge xiaozhi-esp32-server
[ -n "$TUNNEL_SERVICES" ] || docker rm -f wg >/dev/null 2>&1 || true
echo "OTA: http://$LAN_IP:8003/xiaozhi/ota/"
echo "WS:  ws://$LAN_IP:8000/xiaozhi/v1/"
