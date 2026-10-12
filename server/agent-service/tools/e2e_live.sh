#!/usr/bin/env bash
# Live end-to-end gate: tools.e2e_conversation in a one-off agent-service container on the compose network,
# with `docker restart agent-service` between restart-begin and restart-check. Runs on the host of the stack.
# Keys come from server/.env and are never printed.
#   server/agent-service/tools/e2e_live.sh [scenario...]
set -euo pipefail
cd "$(dirname "$0")/../.."
export PATH="$HOME/.rd/bin:$PATH"
set -a; . ./.env; set +a
NET=server_default
URL=http://agent-service:8010
mkdir -p data/e2e
SCENARIOS=("$@")
[ ${#SCENARIOS[@]} -gt 0 ] || SCENARIOS=(short free denied long steer second barge-in restart disconnect)

e2e() {
  docker run --rm --network "$NET" -e AGENT_SERVICE_TOKEN -e HA_MCP_URL -e AGENT_URL="$URL" \
    -v "$PWD/data/e2e:/e2e" -v "$PWD/agent-service/tools:/app/tools:ro" --entrypoint python weixue/agent-service:local -m tools.e2e_conversation "$@" \
    --state /e2e/state.json
}

rc=0
for scenario in "${SCENARIOS[@]}"; do
  if [ "$scenario" = restart ]; then
    e2e restart-begin || rc=1
    docker restart agent-service >/dev/null
    for _ in $(seq 1 45); do docker exec agent-service python -m weixue_agent.health && break; sleep 2; done
    e2e restart-check || rc=1
  else
    e2e "$scenario" || rc=1
  fi
done
exit $rc
