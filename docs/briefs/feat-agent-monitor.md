# feat-agent-monitor — developer brief

Task `feat-agent-monitor`, branch `feat/agent-monitor`, depends on `feat-agent-service`. Sources: DESIGN.md M2 row; `docs/DECISIONS-PENDING.md` 21 (retention), 30 (127.0.0.1, Tailscale later), 35 (no replay without evidence), 36–37, 39–42; `docs/briefs/feat-agent-service.md` rev 4 §10 and §12. Ground truth for data is the agent service's `weixue_agent/store.py` (schema v1) and `monitor.py` as merged; where this brief and the merged code differ, the code wins and the spec says so. Never print secrets. Paths relative to `~/Projects/weixue`.

## 1. Goal

A read-only web board for the agent backend, one page with seven sections:

1. **Timeline**: every tool call (HA, app, memory, server-side web) with args summary, result, duration and, for held HA calls, decision, source (`decided_via`) and time; task and turn transitions incl. steers and cancels; delivery offers and acks.
2. **Cost**: tokens (input, output, cache read/creation), web requests and USD per chat, per task and per day, split by model and turn kind.
3. **Home**: a snapshot of key entities fetched with read-only ha-mcp tools every N minutes (Q1).
4. **Connections**: device (WS session, long-poll), agent service, ha-mcp first byte (bridge stats, service probes), mcp-endpoint, bridge, tunnel.
5. **MCP**: ha-mcp reachable, tool count, pending approvals (ours listed, not-ours counted), policy version and `wait_seconds`.
6. **Chats**: title, summary, mode, open task, last activity, unread.
7. **Errors**: `errors` rows, newest first.

The board never acts: no approve, deny, interrupt, retry or policy write, and no route that could.

## 2. Architecture

```
browser ── 127.0.0.1:8020 ──► agent-monitor (FastAPI, uvicorn 1 worker, uid 1000)
                                ├─ SQLite read-only ◄── ./data/agent-service/db  (agent-service writes, WAL)
                                ├─ GET /healthz, /v1/status ─► agent-service   (monitor token, read scope)
                                ├─ GET /weixue/proactive ────► xiaozhi gateway :8003 (monitor token, read scope)
                                ├─ TCP connect ──────────────► mcp-endpoint-server:8004
                                └─ ./data/mcp-bridge/status/bridge.json (ro)
```

- **Container** `agent-monitor`: `python:3.12-slim` pinned by digest, pinned `requirements.txt`/`.lock` (fastapi, uvicorn, httpx), `pip check`, uid 1000, `restart: unless-stopped`, `mem_limit 128m`, healthcheck on `/healthz`. No `HA_MCP_URL`, API key or `env_file`.
- **Page**: one static `index.html` + JS + CSS, vanilla JS (htmx only if vendored; no CDN).
- **Live updates**: `GET /api/stream` (SSE). One poller checks `PRAGMA data_version` every 1 s and live probes every 2 s, re-queries changed sections and pushes `section` events; full snapshot on connect, heartbeat every 15 s.
- **SQLite read-only**: `file:/db/agent.db?mode=ro`, `PRAGMA query_only=1`, `busy_timeout=2000`, one connection per thread. A WAL reader needs the `-shm` file, so build step 1 checks whether a `:ro` bind mount on Docker Desktop sees new commits within 1 s and survives a `kill -9` of the writer; if not, mount rw and keep `mode=ro` + `query_only` (spec records which). An unknown `PRAGMA user_version` shows a "schema N not supported" banner.
- **Mount scope**: the monitor must not see `/data/memory`. The agent service's database moves to `/data/db/agent.db` (`AGENT_DB`, section 4) and only `./data/agent-service/db` is mounted.
- **Column allowlist**: queries live in one module (`queries.py`) and never select `approvals.ha_token`, `approvals.args_json`, `transcript.*`, `chat_prompts.*`, `idempotency.*`, `decisions.*`, `deliveries.payload_json`, `deliveries.lease_id`. User text (`turns.text`, `final_text`) is shown truncated to 120 chars.
- **Bind**: compose `ports: "127.0.0.1:8020:8020"` on the project network (also in office mode; it reaches agent-service via `AGENT_SERVICE_URL`, `http://wg:8010` with the tunnel). No auth on localhost (30). Times are stored UTC and shown in Europe/Dublin.

## 3. Data contracts

Retention follows 21: the service prunes `chat_events, task_transitions, tool_calls, usage, connection_events, health_probes, errors, delivery_acks` after `EVENT_RETENTION_DAYS=30`; `chats, tasks, turns, usage_daily` are kept. The monitor stores nothing; day totals older than 30 days come from `usage_daily`, task and chat totals from `tasks.cost_usd` / `turns.cost_usd`.

| Section | Tables · columns | Live source |
|---|---|---|
| Timeline | `tool_calls` (tool_use_id, task_id, chat_id, turn_id, kind, tool, args_summary, target, read_only, status, result_summary, started_at, duration_ms, approval_decision, decided_via, decided_by_device, decided_at) ⋈ `turns` (kind, state, mode, model, terminal_reason, reply_end_reason, interrupted_by, ttft_ms, text≤120) · `task_transitions` · `interactions` (kind, source, state, via, decided_at) · `deliveries` (kind, tier, slot, state, attempt, playback) + `delivery_acks` (outcome, reason, cut_at_ms, received_at) | — |
| Cost | `usage` (ts, task_id, chat_id, turn_kind, mode, effort, model, ttft_ms, tokens ×4, web_search/fetch_requests, cost_usd) · `usage_daily` · `tasks.cost_usd` · `turns.cost_usd` | — |
| Home | `home_snapshot` (new, section 4) | — |
| Connections | `connection_events` · `health_probes` (probe ∈ ha_mcp, mcp_endpoint, tunnel, model_api) | agent `/healthz` + `/v1/status.probes`; gateway `devices{session_open, poll_held, last_poll_end_s}`; endpoint TCP; `bridge.json` |
| MCP | latest `health_probes` ha_mcp `detail_json` (tool_count, pending_count, pending_not_ours, policy_version, wait_seconds) · `approvals` (approval_id, tool, state, decision, via, ha_expires_at, created_at) · `interactions` state='pending' | `/v1/status.ha_mcp` |
| Chats | `chats` (chat_id, device_id, title, summary, mode, open_task_id, last_activity_at, activity_seq, last_read_seq) · `tasks` (state, outcome, side_effects_json) · `devices.current_chat_id` | `/v1/status.actors` (busy, queued) |
| Errors | `errors` (ts, source, code, message, task_id, chat_id, trace_id) | — |

Monitor API (all GET, JSON; no other methods are routed, a test asserts it):

```
GET /                       index.html
GET /healthz                {"ok":bool,"db":"ok|missing|schema","sources":{...}}
GET /api/overview           tiles: connections, mcp, today's cost, open tasks, error count 24 h
GET /api/timeline?task=&chat=&before=<ts>&limit=100
GET /api/tasks/{task_id}    task + turns + tool calls + usage + transitions + deliveries
GET /api/cost?group=chat|task|day&days=30
GET /api/home               entities + fetched_at + stale flag
GET /api/chats              GET /api/errors?limit=100
GET /api/stream             SSE: event: section  data: {"section":"connections","data":{...}}
```

Tile states: green ok; amber degraded (ha-mcp first byte > 2 s, tools stale, bridge on cached tool list); red down or stale (latest evidence older than its limit: device 3 s after the last poll ended with none held; agent `/healthz` failing twice at 2 s; ha_mcp probe `ok=0` or older than 25 s; bridge `state != connected` or file older than 2 × 30 s + 5 s).

## 4. Changes outside the monitor container

Each tested in its owning component; done here after feat-agent-service merges unless it already carries them.

- **agent-service**: `AGENT_DB=/data/db/agent.db` (setup.sh moves an existing `/data/agent.db*` while the service is stopped); `HEALTH_PROBE_S 10` for the ha_mcp ping with a 5 s timeout (Q2); a `health_probes` row only on an `ok` change or every 300 s, with `/v1/status.probes` carrying every probe; a read scope: `MONITOR_TOKEN` accepted only on `GET /v1/status` and `GET /v1/tasks/{id}/trace`; the `home_snapshot` table and job (Q1), schema v2 migration.
- **xiaozhi gateway** (`server/overlay/weixue_proactive.py`): `summary()` adds `poll_held` and `last_poll_end_seconds_ago`; `GET /weixue/proactive` also accepts `MONITOR_TOKEN` (read only; every other route keeps `WEIXUE_PROACTIVE_TOKEN`). If the gateway does not yet `POST /v1/devices/{id}/connection` on WS open/close, add it here so `connection_events` has history.
- **mcp-bridge**: write `/app/status/bridge.json` atomically (tmp + rename) on endpoint connect/close, each heartbeat and on SIGTERM (`state: stopped`), with the `ha-mcp stats` fields (session label, sessions_opened, ping first byte p50/max, tools_list first byte/total, tools, tools_age_s) plus `state`, `announced`, `last_ping_ok_at`, `ts`. No URLs, no session ids beyond the existing label.

## 5. UI sketch

Dark theme from the device panel (`monitor_panel.cc`): background `#000000`, cards `#0B1416` with a `#2E6670` hairline, labels `#6E8A92`, secondary `#4E6268`, values `#9FB4BC`, ok `#22B5C4`, warn `#C8963C`, bad `#C05A50`; system UI font, tabular numerals. Phone first (360 px, one column), two columns from 900 px; 16 px gutters.

- **Top strip**: tiles Device · Agent · ha-mcp (first byte ms) · Bridge · Endpoint · Tunnel (hidden when `WG_TUNNEL` is off), each a dot, a word, the evidence age and, when red, the last error code.
- **MCP card**: reachable, tools 77, policy v·wait_seconds, pending: ours N (list: tool, target, age, expires in) · not ours M (count only, "left for you in HA", per decision 18).
- **Timeline**: grouped by task (title, state chip, mode, model, duration, cost); expanded: turns (kind chip, terminal reason), tool calls (tool, target, args summary, status, ms; held calls "approved by panel 14:02" / "denied by voice"), deliveries (tier, state, ack). Filters: chat, state, kind; paged by `before`.
- **Cost**: today / 7 d / 30 d totals, a 30-day bar chart by model, tables per chat and task (tokens, cache hit %, web requests, USD).
- **Home**: entity, state, last changed, fetched-at; "stale" when older than 2 N. **Chats** and **Errors**: lists, newest first.

## 6. Acceptance

1. After one long task (the agent-service e2e scenario 2) the board shows every tool call of that task (count equals `tool_calls` rows and the task's `/v1/tasks/{id}/trace`), its turns, and tokens and cost equal to `SUM(usage)` for the task, live via SSE without reload.
2. Pulling the device (USB and battery off) turns Device red within 30 s; stopping `mcp-bridge` turns Bridge red within 30 s (`docker stop`); stopping `wg` in office mode, or blocking ha-mcp, turns ha-mcp and MCP red within 30 s; each returns green within 30 s of recovery. Timestamps of the action and the first red SSE event are logged.
3. `curl` from another LAN host to `<LAN_IP>:8020` is refused; `127.0.0.1:8020` answers. Opens on a phone through `ssh -L 8020:127.0.0.1:8020` to the Mac; on the Ubuntu server later through Tailscale (`tailscale serve` to 127.0.0.1:8020, no bind change; that step is Jay's).
4. No non-GET route exists; no response or log contains `HA_MCP_URL`, tokens, `ha_token` or `args_json`.

## 7. Test plan

- **Unit** (`server/agent-monitor/tests/`, no network): a fixture DB built from the merged schema (import `weixue_agent.store`, so drift fails) seeded with a long task, an approved and a denied held call, a steer, a cancel, a cut-off delivery and a 40-day-old `usage` row; tests for every query and aggregation, Dublin day boundaries across DST, the column allowlist (response scan for forbidden keys), tile rules with a fake clock, `data_version` change detection, the schema banner, `INSERT` failing, GET-only routes.
- **Component**: the agent-service, gateway and bridge changes in their own suites (`server/tests/`, `server/agent-service/tests/`, bridge tests).
- **e2e (scratch)**: the service's `tools/sim_device.py` + `tools/e2e_conversation.py` in a scratch compose project with the monitor on its volume; Playwright asserts counts and totals after the task, then stops the sim's poll and the bridge and times the red tiles; screenshots at 360 and 1280 px in the spec.
- **Live** (once agent-service is deployed): acceptance 1–2 on the real stack, redacted log `docs/logs/agent-monitor-<date>.log`; no audio (office-mode safe).

## 8. Compose and setup.sh

- One `agent-monitor` service in `server/docker-compose.yml`: build `./agent-monitor`, `ports: ["127.0.0.1:8020:8020"]`, `environment:` only `AGENT_SERVICE_URL`, `MONITOR_TOKEN`, `GATEWAY_URL=http://xiaozhi-esp32-server:8003`, `MCP_ENDPOINT=mcp-endpoint-server:8004`, `WG_TUNNEL`, `TZ`; volumes `./data/agent-service/db:/db` (`:ro` per step 1) and `./data/mcp-bridge/status:/bridge:ro`; `depends_on: agent-service (service_started)`.
- `setup.sh`: generates `MONITOR_TOKEN` once into `server/.env` (0600, never printed), creates `data/agent-service/db` and `data/mcp-bridge/status`, adds `agent-monitor` to `SERVICES`/`IMAGES`, and a readiness wait on `http://127.0.0.1:8020/healthz`. A first deploy has no previous image, so rollback removes it (same rule as agent-service). The monitor failing readiness rolls back like any other service.
- `server/tests/test_setup_scripts.py` stays green; add: token generated once and never printed, monitor bound to 127.0.0.1 only, first-deploy rollback removes it, the db move runs only with the service stopped.

## 9. Docs

`docs/server.md`: an "Agent monitor" section (tiles and red rules, opening locally and over SSH, Tailscale later, retention). The spec lists measured red times.

## 10. Questions for Jay (external services; build waits on Q1, Q2 defaults otherwise)

- **Q1 Home snapshot.** (a) agent-service calls `ha_get_state` for a fixed entity list (`server/policy/monitor_entities.json`, ≤20 entities) every 5 min, only tools the effective policy leaves free, results in `home_snapshot`, outside any chat (recommended: one place holds the secret path, the monitor stays a reader); (b) one `ha_get_overview` every 5 min; (c) no snapshot in v1.
- **Q2 ha-mcp probe cadence.** The 30 s red rule needs the service's ha-mcp ping every 10 s (today 30 s; the bridge's own 30 s heartbeat unchanged). (a) 10 s ping, 5 s timeout (recommended); (b) keep 30 s and relax the acceptance to 45 s.
