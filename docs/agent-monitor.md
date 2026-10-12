# Agent monitor (`server/agent-monitor/`)

A read-only web board for the agent backend: one FastAPI process (`python -m agent_monitor`, uvicorn, one worker, uid 1000) serving one static page (vanilla JS, no CDN) and a GET-only JSON API. It never acts: there is no approve, deny, interrupt, retry or policy route. Contract: `docs/briefs/feat-agent-monitor.md`, DESIGN.md M2, decisions 21, 30, 48, 49.

## Open it

- On the Mac: `http://127.0.0.1:8020/` (compose binds `127.0.0.1:8020` only, decision 30; no auth on localhost).
- From a phone: `ssh -L 8020:127.0.0.1:8020 <mac>` and open `http://127.0.0.1:8020/`.
- On the Ubuntu server later: `tailscale serve` to `127.0.0.1:8020` (no bind change).

## Sections

| Section | Source |
|---|---|
| Tiles: Device · Agent · ha-mcp · Bridge · Endpoint | gateway `GET /weixue/proactive` devices; agent `/healthz` and `/v1/status.probes`; latest `health_probes` row when `/v1/status` fails; `bridge.json`; TCP connect to `mcp-endpoint-server:8004` |
| MCP | reachable, tool count, policy version and `wait_seconds`, pending ours (`approvals` held, with target, age, expiry) and not ours (count from the ha_mcp probe, "left for you in HA") |
| Timeline | tasks newest first with turns, every tool call (args summary, result, duration, held-call decision, `decided_via`, time), task transitions incl. steers and cancels, interactions, deliveries and acks; filters chat, state, tool kind; paged by `before` |
| Cost | today / 7 d / 30 d, a 30-day bar chart by model, tables per chat, task and day (tokens, cache hit %, web requests, USD) |
| Chats, Errors | `chats` (+ live busy/queued from `/v1/status.actors`), `errors` newest first |
| Home | `home_snapshot` and `home_snapshot_meta` (decision 48; `MONITOR_HOME=0` hides it): entity name, state, key attributes, time since the state last changed (`last_changed`, else `updated_at`), the fetch age of the snapshot, "stale" after two periods (`period_s` of the meta row, 300 s at the default) and the last failure code; no actions |

Times are stored UTC and shown in `TZ` (Europe/Dublin when unset or UTC); cost days are Dublin days. Days older than the oldest `usage` row (retention 30 days, decision 21) come from `usage_daily`, which keeps UTC days, and are marked "(UTC)".

## Tile rules

| Tile | Green | Amber | Red |
|---|---|---|---|
| Device | WebSocket session open or a long-poll held | | 3 s after the last poll ended with none held; gateway unreadable |
| Agent | `/healthz` ok | answers not ok (model or ha-mcp check) | 2 consecutive failed requests (2 s timeout, every 2 s); database check failing |
| ha-mcp | latest ping ok | first byte > 2 s; tool list older than 30 min | ping failed; evidence older than `AGENT_HA_PROBE_S` + 15 s (25 s at the default 10 s) |
| Bridge | `state: connected`, announced | cached tool list; not yet announced | file missing, older than 65 s (2 × 30 s heartbeat + 5 s), or `state` not connected (`stopped` on SIGTERM) |
| Endpoint | TCP connect | | 2 consecutive failed connects |
| Tunnel | latest probe ok | | probe failed or older than the ha-mcp limit |

## Data access

- SQLite `file:/db/agent.db?mode=ro`, `PRAGMA query_only=1`, `busy_timeout=2000`, one connection per thread plus one for `PRAGMA data_version`. The mount `./data/agent-service/db:/db` is read-write: through a `:ro` bind mount on Rancher Desktop (Docker 29.1, 2026-10-11) a WAL reader sees commits within 0.2 s and survives a `kill -9` of the writer, but cannot open the database after the writer closed cleanly (no `-shm` left: "unable to open database file") and fails once during a writer restart; with a writable directory and `mode=ro` both cases read without errors and writes are still refused.
- The monitor sees only `data/agent-service/db/` (not `memory/`) and `data/mcp-bridge/status/` (`:ro`).
- Columns come from the allowlists in `agent_monitor/queries.py`; it never selects `approvals.ha_token|args_json`, `transcript`, `chat_prompts`, `idempotency`, `decisions`, `deliveries.payload_json|lease_id|speak`, `interactions.question_json|action_json|decision_json`. User text is clipped to 120 characters; error messages and probe details have URLs and token-like strings replaced.
- `/v1/status` and the gateway summary are reduced to allowlisted fields before use.
- Home columns: `home_snapshot.entity_id|name|state|attributes_json|last_changed|updated_at` and `home_snapshot_meta.period_s|attempted_at|fetched_at|entity_count|error_code`; names and attribute strings are scrubbed like other text. Schemas 1 to 3 are supported; a database without the home tables (schema 1 or 2) shows an empty tile.
- An unknown `PRAGMA user_version` shows "schema N not supported" and no rows.

## API (all GET)

`/` · `/healthz` (`ok` = database readable with a supported schema) · `/api/overview` · `/api/timeline?task=&chat=&state=&kind=&before=&limit=` · `/api/tasks/{id}` · `/api/cost?group=chat|task|day&days=` · `/api/home` · `/api/chats` · `/api/errors?limit=` · `/api/stream` (SSE `event: section`, a snapshot of every section on connect, then changed sections: the database poller checks `data_version` every 1 s, live sources every 2 s; heartbeat every 15 s).

## Deployment

Compose service `agent-monitor`: image `weixue/agent-monitor:local` (`python:3.12-slim` by digest, `requirements.lock` with `--no-deps` + `pip check`), `ports: 127.0.0.1:8020:8020`, environment `AGENT_SERVICE_URL`, `MONITOR_TOKEN`, `GATEWAY_URL`, `MCP_ENDPOINT`, `AGENT_HA_PROBE_S`, `MONITOR_HOME` (default `1`), `TZ` (no `HA_MCP_URL`, no API key, no `env_file`), `mem_limit 128m`, healthcheck `python -m agent_monitor.health`. `scripts/setup.sh` generates `MONITOR_TOKEN` once into `server/.env` (never printed), moves an existing `data/agent-service/agent.db*` into `data/agent-service/db/` while agent-service is stopped, builds and recreates the monitor with the stack and waits for its healthcheck; a first deployment that fails rolls back without it.

`MONITOR_TOKEN` is a read scope: agent-service accepts it on `GET /v1/status` and `GET /v1/tasks/{id}/trace` only, the gateway on `GET /weixue/proactive` only.

## Pending decisions

- 49 (ha-mcp probe cadence): built with the recommended 10 s ping and 5 s timeout (`AGENT_HA_PROBE_S`, `AGENT_HA_PROBE_TIMEOUT_S` in `server/.env`); `AGENT_HA_PROBE_S=30` gives option b (red after 45 s).

## Tests

`cd server/agent-monitor && python -m pytest` (fixture database built with `weixue_agent.store`, fake agent and gateway over `httpx.MockTransport`): queries and aggregations, Dublin days across the DST change, the 40-day row and `usage_daily` fallback, the column allowlist and a response scan for forbidden keys and seeded secrets, tile rules with a fake clock (bridge down via the status-file age), `data_version` change detection and SSE frames, the schema banner, `INSERT` failing, GET-only routes, the home tile (rows from fixture data, scrubbing, stale after two periods, failure code, a database without the home tables, default on).
