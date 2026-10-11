#!/usr/bin/env bash
# Idempotent bootstrap: pre-flight, model download, config render, image build, validation, atomic
# publish, start, deployment readiness; a failed start or check restores the previous deployment.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.rd/bin:$PATH"

[ -f .env ] || { echo "missing server/.env (copy .env.example)"; exit 1; }
set -a; . ./.env; set +a
# Pre-flight: every key server/.env must set; nothing is built, published or started before it passes.
REQUIRED_KEYS=(LAN_IP BIND_IP ALLOWED_DEVICES TZ TZ_OFFSET ANTHROPIC_API_KEY GOOGLE_SA_FILE HA_MCP_URL)
MISSING=()
for key in "${REQUIRED_KEYS[@]}"; do [ -n "${!key:-}" ] || MISSING+=("$key"); done
if [ ${#MISSING[@]} -gt 0 ]; then
  echo "server/.env must set ${MISSING[*]} (required: ${REQUIRED_KEYS[*]}; see .env.example and docs/server.md)"
  exit 1
fi
[ -f "$GOOGLE_SA_FILE" ] || { echo "GOOGLE_SA_FILE does not exist"; exit 1; }
# BIND_IP: 0.0.0.0 or 127.0.0.1; on Linux also an IPv4 address on a local interface.
case "$BIND_IP" in
  0.0.0.0 | 127.0.0.1) ;;
  *)
    if [ "$(uname -s)" = Darwin ]; then
      echo "BIND_IP=$BIND_IP: on macOS use 0.0.0.0 or 127.0.0.1 in server/.env (docs/server.md)"
      exit 1
    fi
    LOCAL_IPV4=$(ip -o -4 addr 2>/dev/null | awk '{ split($4, a, "/"); print a[1] }' || true)
    case $'\n'"$LOCAL_IPV4"$'\n' in
      *$'\n'"$BIND_IP"$'\n'*) ;;
      *)
        echo "BIND_IP=$BIND_IP is not 0.0.0.0, 127.0.0.1 or an IPv4 address on a local interface (docs/server.md)"
        exit 1
        ;;
    esac
    ;;
esac

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

# agent-service: bearer token generated once into server/.env (never printed); its host is wg while the
# service shares the tunnel namespace.
if [ -z "${AGENT_SERVICE_TOKEN:-}" ]; then
  AGENT_SERVICE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  [ -z "$(tail -c 1 .env)" ] || echo >> .env
  printf 'AGENT_SERVICE_TOKEN=%s\n' "$AGENT_SERVICE_TOKEN" >> .env
  echo "generated AGENT_SERVICE_TOKEN in server/.env"
fi
export AGENT_SERVICE_TOKEN
# agent-monitor's read-only scope on agent-service and the gateway; generated once, never printed.
if [ -z "${MONITOR_TOKEN:-}" ]; then
  MONITOR_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
  [ -z "$(tail -c 1 .env)" ] || echo >> .env
  printf 'MONITOR_TOKEN=%s\n' "$MONITOR_TOKEN" >> .env
  echo "generated MONITOR_TOKEN in server/.env"
fi
export MONITOR_TOKEN
AGENT_HOST_WANT=$([ -n "$TUNNEL_SERVICES" ] && echo wg || true)
if [ "$(sed -n 's/^AGENT_SERVICE_HOST=//p' .env | tail -n 1)" != "$AGENT_HOST_WANT" ]; then
  ENV_REST=$(grep -v '^AGENT_SERVICE_HOST=' .env || true)
  { printf '%s\n' "$ENV_REST"; [ -z "$AGENT_HOST_WANT" ] || echo "AGENT_SERVICE_HOST=$AGENT_HOST_WANT"; } > .env
fi
if [ -n "$AGENT_HOST_WANT" ]; then export AGENT_SERVICE_HOST=$AGENT_HOST_WANT; else unset AGENT_SERVICE_HOST; fi

MODEL=data/models/SenseVoiceSmall/model.pt
MODEL_URL=https://modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt
mkdir -p data/models/SenseVoiceSmall data/mcp-endpoint data/mcp-bridge/cache data/mcp-bridge/status \
  data/agent-service/db
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

CONFIG=data/.config.yaml
SERVICES=($TUNNEL_SERVICES mcp-bridge agent-service agent-monitor xiaozhi-esp32-server)
IMAGES=(weixue/mcp-bridge:local weixue/agent-service:local weixue/agent-monitor:local weixue/xiaozhi-esp32-server:local)
[ -z "$TUNNEL_SERVICES" ] || IMAGES+=(weixue/wg:local)
ready_host() { if [ "$BIND_IP" = 0.0.0.0 ]; then echo 127.0.0.1; else echo "$BIND_IP"; fi; }

umask 077
STAGED=$(mktemp "$CONFIG.XXXXXX")
trap 'rm -f "$STAGED"' EXIT

# The endpoint key, the URL derived from it and its two consumers (the server config and the
# bridge's URL file) are one deployment unit: rollback restores all of them.
ENDPOINT_CFG=data/mcp-endpoint/.mcp-endpoint-server.cfg
ENDPOINT_URL=data/mcp-bridge/endpoint-url
for file in "$ENDPOINT_CFG" "$ENDPOINT_URL"; do
  rm -f "$file.rollback"
  if [ -f "$file" ]; then cp -p "$file" "$file.rollback"; fi
done

docker compose up -d mcp-endpoint-server
MCP=""
for _ in $(seq 1 30); do
  MCP=$(docker logs mcp-endpoint-server 2>&1 | sed $'s/\x1b\\[[0-9;]*m//g' | grep -o 'ws://[^ ]*/mcp_endpoint/mcp/?token=[^ ]*' | tail -1 || true)
  [ -n "$MCP" ] && break; sleep 1
done
[ -n "$MCP" ] || { echo "mcp endpoint URL not found in logs"; exit 1; }
chmod 600 data/mcp-endpoint/.mcp-endpoint-server.cfg
MCP_ENDPOINT_INTERNAL=$(echo "$MCP" | sed -E 's#^ws://[^/]+#ws://mcp-endpoint-server:8004#')

MCP_ENDPOINT=$MCP_ENDPOINT_INTERNAL python3 scripts/render_config.py config/config.template.yaml > "$STAGED"

# A first deployment of agent-service or agent-monitor has no previous version: a rollback removes it.
AGENT_REQUIRED=1
AGENT_PREVIOUS=$(docker image inspect weixue/agent-service:local >/dev/null 2>&1 && echo 1 || true)
MONITOR_REQUIRED=1
MONITOR_PREVIOUS=$(docker image inspect weixue/agent-monitor:local >/dev/null 2>&1 && echo 1 || true)
for image in "${IMAGES[@]}"; do
  if docker image inspect "$image" >/dev/null 2>&1; then
    docker tag "$image" "${image%:*}:rollback"
  fi
done

restore_images() {
  local rc=0
  for image in "${IMAGES[@]}"; do
    if docker image inspect "${image%:*}:rollback" >/dev/null 2>&1; then
      docker tag "${image%:*}:rollback" "$image" || rc=1
    fi
  done
  return $rc
}

# .env of the last successful run (0600); rollback restores it and keeps the attempted one as .env.failed.
ENV_DEPLOYED=.env.deployed
DEPLOY_WAIT_S=${DEPLOY_WAIT_S:-90}

# Runs "$@" every 2 s until it passes; after DEPLOY_WAIT_S fails with the last line it printed.
wait_for() {
  local what=$1 end=$((SECONDS + DEPLOY_WAIT_S)) out
  shift
  until out=$("$@" 2>&1); do
    if [ "$SECONDS" -ge "$end" ]; then
      echo "$what not ready after ${DEPLOY_WAIT_S}s$(printf '%s' "$out" | tail -n 1 | sed 's/^./: &/')"
      return 1
    fi
    sleep 2
  done
}

# Deployment readiness: OTA and WebSocket banners, the tunnel (WG_TUNNEL=1: handshake and
# boundary), the bridge's health file, one read-only Home Assistant call made from the
# server container through the endpoint, the bridge and ha-mcp (scripts/check_ha_path.py),
# agent-service's healthcheck (database, model API, ha-mcp ping) and agent-monitor's /healthz (database readable).
deploy_ready() {
  python3 scripts/check_ready.py "$(ready_host)" || return 1
  if [ "${WG_TUNNEL:-}" = 1 ]; then
    wait_for "wg tunnel" docker exec wg /usr/local/bin/healthcheck.sh || return 1
  fi
  wait_for mcp-bridge docker exec mcp-bridge python -m weixue_bridge.health || return 1
  wait_for "Home Assistant via the voice path" \
    sh -c 'docker exec -i xiaozhi-esp32-server python - --timeout 20 < scripts/check_ha_path.py' || return 1
  if [ -n "$AGENT_REQUIRED" ]; then
    wait_for agent-service docker exec agent-service python -m weixue_agent.health || return 1
  fi
  if [ -n "$MONITOR_REQUIRED" ]; then
    wait_for agent-monitor docker exec agent-monitor python -m agent_monitor.health || return 1
  fi
  echo "ready: bridge healthy$([ "${WG_TUNNEL:-}" != 1 ] || echo ", tunnel healthy"), Home Assistant answered via the voice path$([ -z "$AGENT_REQUIRED" ] || echo ", agent-service healthy")$([ -z "$MONITOR_REQUIRED" ] || echo ", agent-monitor healthy")"
}

manual_restore() {
  local profiles=${COMPOSE_PROFILES:-} flags=""
  for profile in ${profiles//,/ }; do flags+=" --profile $profile"; done
  echo "rollback failed: the stack is down; manual restore:"
  echo "  cd $PWD"
  for image in "${IMAGES[@]}"; do
    if docker image inspect "${image%:*}:rollback" >/dev/null 2>&1; then
      echo "  docker tag ${image%:*}:rollback $image"
    fi
  done
  echo "  docker compose$flags up -d --force-recreate $*"
  echo "docker-compose.yml is not rolled back; if that up fails the same way, correct it and server/.env first."
}

# Restores the previous config, endpoint unit, .env and images and recreates the containers; always exits 1.
rollback() {
  echo "$1; rolling back"
  if [ -f "$CONFIG.rollback" ]; then mv -f "$CONFIG.rollback" "$CONFIG"; fi
  if [ -f "$ENDPOINT_URL.rollback" ]; then mv -f "$ENDPOINT_URL.rollback" "$ENDPOINT_URL"; fi
  local endpoint=""
  if [ -f "$ENDPOINT_CFG.rollback" ] && ! cmp -s "$ENDPOINT_CFG.rollback" "$ENDPOINT_CFG"; then
    cp -p "$ENDPOINT_CFG.rollback" "$ENDPOINT_CFG"
    endpoint=mcp-endpoint-server
    echo "endpoint key restored from the last run"
  fi
  if [ -f "$ENV_DEPLOYED" ]; then
    cp -p .env .env.failed
    chmod 600 .env.failed
    for key in $(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' .env); do unset "$key"; done
    unset COMPOSE_PROFILES
    cp -p "$ENV_DEPLOYED" .env
    set -a; . ./.env; set +a
    echo "server/.env restored from the last successful run; the attempted one is server/.env.failed"
  fi
  restore_agent_db
  local services=($endpoint mcp-bridge)
  if [ -n "$AGENT_PREVIOUS" ]; then
    services+=(agent-service)
  else
    AGENT_REQUIRED=
    docker rm -f agent-service >/dev/null 2>&1 || true
  fi
  if [ -n "$AGENT_PREVIOUS" ] && [ -n "$MONITOR_PREVIOUS" ]; then
    services+=(agent-monitor)
  else
    MONITOR_REQUIRED=
    docker rm -f agent-monitor >/dev/null 2>&1 || true
  fi
  services+=(xiaozhi-esp32-server)
  [ "${WG_TUNNEL:-}" != 1 ] || services=(wg "${services[@]}")
  if restore_images && docker compose up -d --force-recreate "${services[@]}"; then
    [ "${WG_TUNNEL:-}" = 1 ] || docker rm -f wg >/dev/null 2>&1 || true
    deploy_ready || echo "previous version is not ready either"
  else
    manual_restore "${services[@]}"
  fi
  exit 1
}

if ! docker compose build "${SERVICES[@]}"; then
  restore_images
  echo "image build failed; $CONFIG and the images are unchanged"
  exit 1
fi
if ! docker run --rm --entrypoint python \
    -v "$PWD/$STAGED:/tmp/config.yaml:ro" \
    -v "$PWD/scripts/validate_config.py:/tmp/validate_config.py:ro" \
    weixue/xiaozhi-esp32-server:local /tmp/validate_config.py /tmp/config.yaml; then
  restore_images
  echo "rendered config is invalid; $CONFIG and the images are unchanged"
  exit 1
fi

# agent-service's database moves into data/agent-service/db/ (the directory agent-monitor mounts) while the
# service is stopped; compose sets AGENT_DB for this and for a rolled-back image.
AGENT_DATA=data/agent-service
if [ -f "$AGENT_DATA/agent.db" ] && [ ! -e "$AGENT_DATA/db/agent.db" ]; then
  docker stop agent-service >/dev/null 2>&1 || true
  if [ "$(docker inspect -f '{{.State.Running}}' agent-service 2>/dev/null || echo false)" != false ]; then
    restore_images
    echo "agent-service is still running; its database was not moved and nothing was published"
    exit 1
  fi
  for file in agent.db agent.db-wal agent.db-shm; do
    if [ -e "$AGENT_DATA/$file" ]; then mv "$AGENT_DATA/$file" "$AGENT_DATA/db/$file"; fi
  done
  echo "agent-service database moved to server/$AGENT_DATA/db/"
fi

# agent-service's database as it was before this run, copied with the service and the monitor stopped (a newer
# image migrates it in place); rollback() puts it back and keeps the attempted one in db-failed/.
backup_agent_db() {
  local file
  rm -rf "$AGENT_DATA/db-rollback"
  [ -f "$AGENT_DATA/db/agent.db" ] || return 0
  docker stop agent-service agent-monitor >/dev/null 2>&1 || true
  [ "$(docker inspect -f '{{.State.Running}}' agent-service 2>/dev/null || echo false)" = false ] || return 1
  mkdir -p "$AGENT_DATA/db-rollback"
  for file in agent.db agent.db-wal agent.db-shm; do
    if [ -e "$AGENT_DATA/db/$file" ]; then cp -p "$AGENT_DATA/db/$file" "$AGENT_DATA/db-rollback/$file"; fi
  done
}
restore_agent_db() {
  local file
  [ -f "$AGENT_DATA/db-rollback/agent.db" ] || return 0
  docker stop agent-service agent-monitor >/dev/null 2>&1 || true
  rm -rf "$AGENT_DATA/db-failed"
  mkdir -p "$AGENT_DATA/db-failed"
  for file in agent.db agent.db-wal agent.db-shm; do
    if [ -e "$AGENT_DATA/db/$file" ]; then mv "$AGENT_DATA/db/$file" "$AGENT_DATA/db-failed/$file"; fi
    if [ -e "$AGENT_DATA/db-rollback/$file" ]; then cp -p "$AGENT_DATA/db-rollback/$file" "$AGENT_DATA/db/$file"; fi
  done
  echo "agent-service database restored from before this run; the attempted one is server/$AGENT_DATA/db-failed/"
}
if ! backup_agent_db; then
  restore_images
  echo "agent-service is still running; its database was not backed up and nothing was published"
  exit 1
fi

ENDPOINT_FILE=$(mktemp "$ENDPOINT_URL.XXXXXX")
printf '%s\n' "$MCP_ENDPOINT_INTERNAL" > "$ENDPOINT_FILE"
mv -f "$ENDPOINT_FILE" "$ENDPOINT_URL"
if [ -f "$CONFIG" ]; then cp -p "$CONFIG" "$CONFIG.rollback"; fi
mv -f "$STAGED" "$CONFIG"

docker compose up -d --force-recreate "${SERVICES[@]}" || rollback "docker compose up failed"
[ -n "$TUNNEL_SERVICES" ] || docker rm -f wg >/dev/null 2>&1 || true
deploy_ready || rollback "new version is not ready"
cp .env "$ENV_DEPLOYED"
chmod 600 "$ENV_DEPLOYED"
rm -f .env.failed "$ENDPOINT_CFG.rollback" "$ENDPOINT_URL.rollback"
echo "OTA: http://$LAN_IP:8003/xiaozhi/ota/"
echo "WS:  ws://$LAN_IP:8000/xiaozhi/v1/"
echo "Monitor: http://127.0.0.1:8020/"
