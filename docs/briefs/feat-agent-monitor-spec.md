# Feature Spec: Agent backend monitor board (M2)
Task: feat-agent-monitor | Branch: feat/agent-monitor | Date: 2026-10-11

## Summary
New `agent-monitor` container: FastAPI on `127.0.0.1:8020`, one vanilla-JS page fed by SSE, GET routes only. Reads the agent-service SQLite read-only, plus agent `/healthz` + `/v1/status`, the gateway summary, an endpoint TCP probe and the bridge status file.

## Changes
- `server/agent-monitor/`: `queries.py` with a column allowlist (user text clipped to 120 chars, URLs scrubbed), tile rules, SSE hub driven by `data_version`, page with timeline, cost, MCP, chats and errors. Home tile is off unless `MONITOR_HOME=1`.
- agent-service: `MONITOR_TOKEN` read scope (only `GET /v1/status` and `GET /v1/tasks/{id}/trace`). ha-mcp ping every 10 s with a 5 s timeout (`AGENT_HA_PROBE_S`/`_TIMEOUT_S`). A probe row is written when `ok` flips, or every 300 s. DB moved to `/data/db/agent.db`.
- Gateway: summary gains `poll_held` and `last_poll_end_seconds_ago`; `MONITOR_TOKEN` works on `GET /weixue/proactive` only; WS open/close is posted to `/v1/devices/{id}/connection`.
- Bridge: writes `bridge.json` atomically on connect/close, after each ping, and with `stopped` on SIGTERM.
- compose, `setup.sh` (token generated once and never printed, DB move only while agent-service is stopped, readiness check, rollback on first deploy), `docs/agent-monitor.md`, docs.

## Design Decisions
- WAL step 1: a `:ro` mount fails after a clean writer shutdown ("unable to open database file"). The DB directory is therefore mounted rw, and the monitor opens it with `mode=ro` + `query_only`.
- Decision 48 is still open, so there is no `home_snapshot` table or job. A schema-v2 migration would break rollback.
- The ha-mcp red limit is the probe period + 15 s (25 s now, 45 s if `AGENT_HA_PROBE_S=30`).

## Problems & Resolutions
- `data_version` is per connection: read on one dedicated connection.

## Open Questions
- 48 and 49 undecided; defaults built.

## Test Coverage
Tests: monitor 38, agent-service 103, server 799 pass. Covers a fixture DB from `weixue_agent.store`, Dublin DST days, `usage_daily` fallback, a secret scan of responses, GET-only routes, "bridge down" via status-file age, setup.sh token/bind/rollback/DB-move. Live (`docs/logs/agent-monitor-2026-10-11.log`): setup.sh deploy under the lock; a real `ha_get_state` turn appeared on the open page without reload, tool calls and tokens equal to the DB; device tile matches the gateway; LAN `:8020` no answer.

**Deferred:** timed red/green cycles of acceptance 2 (unplug, bridge stop, ha-mcp block; no live stops allowed), acceptance 3 phone/Tailscale, the long multi-tool task.

**Note:** the live stack now mounts this worktree's `server/data`. Redeploy main with setup.sh after merge.
