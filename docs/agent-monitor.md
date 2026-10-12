# Agent monitor (`server/agent-monitor/`)

A read-only web board for the agent backend: one FastAPI process (`python -m agent_monitor`, uvicorn, one worker, uid 1000) serving one static page (vanilla JS, no CDN, vendored IBM Plex fonts) and a GET-only JSON API with an SSE stream. It never acts: there is no approve, deny, interrupt, retry or policy route; a held call says where to decide. Contract: `docs/briefs/feat-agent-monitor.md`, DESIGN.md M2, decisions 21, 30, 48, 49, 78, 79. Design: `~/Projects/research/reports/weixue-monitor-ui-v1.md` (Logbook with a verdict); screenshots `docs/logs/monitor-ui-v2-*.png`.

## Open it

The page and every `/api/` route need `MONITOR_TOKEN` (from `server/.env`) when it is set, as `?token=<MONITOR_TOKEN>` or `Authorization: Bearer <MONITOR_TOKEN>`; the page keeps the query token for its fetches and the SSE URL. `/healthz` and `/static/` are open. `setup.sh` always sets a token.

- On the server: compose binds `${MONITOR_BIND:-127.0.0.1}:8020`. With `MONITOR_BIND=192.168.8.125` in the server's `.env` the board is at `http://192.168.8.125:8020/?token=<MONITOR_TOKEN>` from the LAN and the VPN (decision 79, plain http).
- With the default bind: `ssh -L 8020:127.0.0.1:8020 jay@192.168.8.125`, then `http://127.0.0.1:8020/?token=<MONITOR_TOKEN>`.
- Query options: `embed=1` (no brand, clock, theme toggle or footer), `theme=light|dark` (default: follows `prefers-color-scheme`; the Auto / Light / Dark toggle is remembered per browser), `state=` and `kind=` (the timeline API filters), `#task=<task_id>` (opens that task's trace).

## Home Assistant dashboard card

Server `.env`: `MONITOR_BIND=192.168.8.125` and `MONITOR_FRAME_ANCESTORS=http://192.168.8.142:8123` (the CSP `frame-ancestors`; default `'none'`, which forbids framing). Then a Webpage card (or a dashboard of type Webpage):

```yaml
type: iframe
url: http://192.168.8.125:8020/?embed=1&token=<MONITOR_TOKEN>
aspect_ratio: 120%
```

The iframe URL holds the token, so anyone who can edit or inspect that dashboard can read the board. The same token is the read scope on agent-service `GET /v1/status` and the gateway `GET /weixue/proactive` (port 8003 on the LAN).

## Layout

| Region | Shows | Source |
|---|---|---|
| Verdict bar (sticky) | `All good` / `Needs a look` and one sentence; amber only while a call is held for us or a connection is down (`overview.verdict`); `Live · N s` (amber after 30 s without an SSE message or heartbeat); clock | `overview`, `problems`, newest task |
| Held-approval card | only while `mcp.pending_ours` is non-empty: what was said, tool, target, held / expires meter, "decide on the device panel or in Home Assistant", policy version, calls left for you in HA | `overview.mcp` |
| Connections | `All N up` / `N down` / `N degraded`, one dot per tile with latency or word; `24 h history`: lanes per stored probe (ha-mcp, Endpoint, Model API, Tunnel) and per-tile facts (no device MACs) | `overview.tiles`, `/api/health/history` |
| Approvals | `None waiting` / `N waiting`, the last decision (task, approved / denied, where, when), hold seconds, tool count, calls left in HA | `overview.mcp` |
| Problems · 24 h | failures and notices counted separately by the severity map, top groups | `problems` |
| Today | cost, voice turns, tokens in, cache %, 7-day cost bars (Dublin days) | `overview.counts`, `cost` |
| Home | lights on, temperatures, covers and locks, snapshot age (`stale` after two periods) | `home` (`MONITOR_HOME=0` hides it) |
| Since you last looked | voice turns and chats, HA changes, decided approvals, failures (home snapshots "and recovered"), cancelled; a 24 h activity strip with the last-look marker | `/api/since` |
| Filter bar | search over what was said, the title and the reply; Everything / HA actions / Approvals / Failed / Cancelled with task counts; chat; Lifecycle events; `N new · jump to top` | `/api/timeline` |
| Feed (by day) | per task: what was said, the reply, tool steps (`HA`, `SRV`, `APP`, `MEM`, `DEV`, `HOLD`), steers, outcome (`failed · reason` vs `cancelled by you`), chat and delivery, cost, duration, tokens; repeats of the same question in a chat within 15 min fold to `×N`; failures without a task appear as their own rows; notices and lifecycle steps (transitions, interactions, deliveries, acks) only with the Lifecycle toggle | `timeline`, `problems` |
| Trace (per task) | six stats, Trace / Conversation / Raw, a waterfall of turns (first-token tick), tool calls (policy-gate diamond at the decision), deliveries; inspector with arguments and result; token split (cache read / input not cached / output); result tier, side effects, retry, task and trace ids | `/api/tasks/{id}` |
| Rail | chats (unread dot, current, click filters the feed), problems with `open trace`, home by room (`area` attribute, else the room word in the entity id) | `chats`, `problems`, `home` |

Below 900 px the page is one column (16 px gutter): connection dots, a 2×2 of Approvals / Problems / Today / Home, the summary, search and segments (scrolling inside their box), the feed, then the rail. Glyph shapes carry state: filled dot done or up, ring with dot in flight, diamond held, triangle failure, hollow ring cancelled, square folded repeats, dotted ring notice. Text colour tokens are at least 4.5:1 on every fill in both themes (`tests/test_board_v2.py`).

Live updates: the feed re-renders keeping the first visible item in place; new tasks arriving while scrolled down wait behind the `N new` pill. The last look is stored per browser (`localStorage` `wx-last-seen`, written when the page is hidden); without it the summary starts at local midnight.

## Severity map

`agent_monitor/severity.py`: `(model, transcript)`, `(model, model_fallback)` and `(model, retention)` are notices; every other `(source, code)` is a failure. Notices never change the verdict or the failure counts. A JSON message's `reason` (e.g. `prefix_binding_mismatch`) labels a notice group.

## Tile rules

| Tile | Green | Amber | Red |
|---|---|---|---|
| Device | WebSocket session open or a long-poll held | | 3 s after the last poll ended with none held; gateway unreadable |
| Agent | `/healthz` ok | answers not ok (model or ha-mcp check) | 2 consecutive failed requests (2 s timeout, every 2 s); database check failing |
| ha-mcp | latest ping ok | first byte > 2 s; tool list older than 30 min | ping failed; evidence older than `AGENT_HA_PROBE_S` + 15 s (25 s at the default 10 s) |
| Bridge | `state: connected`, announced | cached tool list; not yet announced | file missing, older than 65 s (2 × 30 s heartbeat + 5 s), or `state` not connected (`stopped` on SIGTERM) |
| Endpoint | TCP connect | | 2 consecutive failed connects |
| Tunnel | latest probe ok | | probe failed or older than the ha-mcp limit |

Red is "down" for the verdict; amber is "degraded" and stays in the `All good` sentence.

Lanes: each `health_probes` row is ok or bad until the next row; a gap longer than 660 s without a row is unknown.

## Data access

- SQLite `file:/db/agent.db?mode=ro`, `PRAGMA query_only=1`, `busy_timeout=2000`, one connection per thread plus one for `PRAGMA data_version`. The mount `./data/agent-service/db:/db` is read-write: through a `:ro` bind mount on Rancher Desktop (Docker 29.1, 2026-10-11) a WAL reader sees commits within 0.2 s and survives a `kill -9` of the writer, but cannot open the database after the writer closed cleanly (no `-shm` left: "unable to open database file") and fails once during a writer restart; with a writable directory and `mode=ro` both cases read without errors and writes are still refused.
- The monitor sees only `data/agent-service/db/` (not `memory/`) and `data/mcp-bridge/status/` (`:ro`).
- Columns come from the allowlists in `agent_monitor/queries.py`; it never selects `approvals.ha_token|args_json`, `transcript`, `chat_prompts`, `idempotency`, `decisions`, `deliveries.payload_json|lease_id|speak`, `interactions.question_json|action_json|decision_json`. User text is clipped to 120 characters; error messages and probe details have URLs and token-like strings replaced. The page never renders device ids.
- `/v1/status` and the gateway summary are reduced to allowlisted fields before use.
- Home columns: `home_snapshot.entity_id|name|state|attributes_json|last_changed|updated_at` and `home_snapshot_meta.period_s|attempted_at|fetched_at|entity_count|error_code`. Schemas 1 to 3 are supported; a database without the home tables shows "No home snapshot yet".
- An unknown `PRAGMA user_version` shows "schema N not supported" and no rows.

Times are stored UTC and shown in `TZ` (Europe/Dublin when unset or UTC); cost days are Dublin days. Days older than the oldest `usage` row (retention 30 days, decision 21) come from `usage_daily`, which keeps UTC days.

## API (all GET)

| Route | Returns |
|---|---|
| `/` | the page (`?embed=1` marks it embedded) |
| `/healthz` | `ok` = database readable with a supported schema |
| `/api/overview` | tiles, `mcp` (held calls ours, not ours, last decision), counts (`turns_today` included), `verdict {state: ok|attention, held, down}` |
| `/api/timeline?chat=&q=&segment=all|ha|approvals|failed|cancelled&state=&kind=&before=&limit=` | tasks newest first with children and `segments` (task counts under the chat and search filters) |
| `/api/tasks/{id}` | one task with children and its `usage` rows |
| `/api/problems?hours=24` | error groups by `(source, code)`: severity, count, first / last time, last task, reasons, `recovered` (home snapshot fetched since), windows of rows ≤ 15 min apart; `failures`, `notices`, `rows`, `severity_map` |
| `/api/since?since=<ISO>&hours=24` | the since-you-last-looked counts from `since` (local midnight when absent, `default: true`) and the activity ticks of the last `hours` |
| `/api/health/history?hours=24` | lanes per stored probe: segments `{from, to, state: ok|bad|unknown}`, last latency, flips |
| `/api/cost?group=chat|task|day&days=`, `/api/home`, `/api/chats`, `/api/errors?limit=` | as before |
| `/api/stream` | SSE `event: section` (`overview`, `timeline`, `cost`, `chats`, `errors`, `problems`, `home`): a snapshot on connect, then changed sections (database `data_version` every 1 s, live sources every 2 s); `event: heartbeat` every 15 s without other messages |

## Deployment

Compose service `agent-monitor`: image `weixue/agent-monitor:local` (`python:3.12-slim` by digest, `requirements.lock` with `--no-deps` + `pip check`), `ports: ${MONITOR_BIND:-127.0.0.1}:8020:8020`, environment `AGENT_SERVICE_URL`, `MONITOR_TOKEN`, `GATEWAY_URL`, `MCP_ENDPOINT`, `AGENT_HA_PROBE_S`, `MONITOR_HOME` (default `1`), `MONITOR_FRAME_ANCESTORS` (default `'none'`; `'self'` and `http(s)://host[:port]` origins), `TZ` (no `HA_MCP_URL`, no API key, no `env_file`), `mem_limit 128m`, healthcheck `python -m agent_monitor.health`. Uvicorn runs without an access log, so query tokens are not logged; responses send `Referrer-Policy: no-referrer`. `scripts/setup.sh` generates `MONITOR_TOKEN` once into `server/.env` (never printed), builds and recreates the monitor with the stack and waits for its healthcheck; a first deployment that fails rolls back without it.

`MONITOR_TOKEN` is a read scope: agent-service accepts it on `GET /v1/status` and `GET /v1/tasks/{id}/trace` only, the gateway on `GET /weixue/proactive` only, the monitor on its page and API.

## Tests

`cd server/agent-monitor && python -m pytest` (fixture database built with `weixue_agent.store`, fake agent and gateway over `httpx.MockTransport`): queries and aggregations, Dublin days across the DST change, `usage_daily` fallback, the column allowlist and a response scan for forbidden keys and seeded secrets, tile rules with a fake clock, `data_version` change detection and SSE frames, the schema banner, GET-only routes, the home tile; `tests/test_board_v2.py`: the token gate (query, bearer, wrong token, open routes, no token set), CSP `frame-ancestors` and its validation, embed mode, no inline styles or scripts, the severity map and problem groups, the verdict, since-you-last-looked counts, segments and search, lanes, and the contrast of every text token on every fill in both themes.
