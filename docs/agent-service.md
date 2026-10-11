# Agent service (`server/agent-service/`)

One asyncio process (`python -m weixue_agent`, uvicorn, one worker) that keeps one append-only Claude conversation per chat, answers quick asks on Sonnet 5.5 and runs tasks on Fable 5.1 in the same conversation, calls ha-mcp through the ADR-9 adapter and reports results through a durable outbox. Contract: `docs/briefs/feat-agent-service.md` (rev 4), DESIGN.md ADR-9/10/11, decisions 15–45. Gate evidence: `docs/logs/feat-agent-service-s1-2026-10-10.md`, `docs/logs/feat-agent-service-e2e-2026-10-10.log`.

## API

Base `http://agent-service:8010/v1` (office mode `http://wg:8010`), bearer `AGENT_SERVICE_TOKEN` on every route but `GET /healthz`; `MONITOR_TOKEN` (agent-monitor) is accepted on `GET /v1/status` and `GET /v1/tasks/{id}/trace` only. Routes, bodies and events as in brief §5; the JSON Schemas are `server/agent-service/schemas/*.schema.json` (written by `python -m tools.export_schemas`, drift fails `test_schemas`). `POST /chats/{id}/messages` with `Accept: text/event-stream` streams the chat's events from `accepted` to this message's `reply_end`; only `text` events of this message's turn are on it. `GET /chats/{id}` adds `epoch`, `mode`, `task_model` and the roles and states of the current epoch's transcript rows (no content). Each text delta up to the end of a reply's first clause (the gateway TTS's `first_clause` rule, `clause.py`) is emitted at once; later deltas are coalesced per 150 ms or sentence end.

## Turn loop

| Item | Value |
|---|---|
| Runner | `client.beta.messages.tool_runner(stream=True, run_tools_eagerly=True)`; every `tool_use` is deferred at its `content_block_start`, so tools run once, after the stream, in `generate_tool_call_response()` |
| Quick | `claude-sonnet-5-5`, effort low; tools: app tools, 7 hot ha-mcp tools loaded, 70 deferred behind `tool_search_tool_bm25_20251119`, `web_search_20260209`, `web_fetch_20260209` (`max_uses` 5) |
| Task | `claude-fable-5-1` (after a retention 400 the chat's task model is `claude-opus-5-5`), effort high; the quick set plus `memory_20250818` on `/data/memory` |
| Every request | `thinking {adaptive, display: updates, block_binding: drop_block}`, top-level effort low, `fallbacks: "default"`, top-level `cache_control`, betas of brief §6 |
| Switch | `start_task` or 3 quick tool rounds: an effort-only `high` system message before the tool results, then a new runner on the task model (each model keeps its own tool set) |
| Memory | each turn's context note lists `path: first line` of each memory file; a quick turn that must save or read more calls `start_task` (finding A in the S1 log) |

The transcript (`transcript` table, `transcript.py`) is append-only per epoch. Rows a request sent stay `tail` until its response completes; only that tail is rewritten, by merging the next turn's user content into it (cancel, barge-in, refused or failed requests). Every unanswered `tool_use` gets a `tool_result` in the next user message (`result unknown: …; do not repeat it` for a non-read-only HA call that was sent, else `not run: …`). Service notes go in one system message after the user's words. A compaction (`compact-2026-09-04`, scheduled when a turn's input exceeds 150k tokens) starts a new epoch with a fresh `chat_prompts` row; `system` and the ha-mcp tool list are frozen per epoch.

## Device actions

App tool `device_action {tool, args}` operates or reads the voice device itself. `tool` is one of the agent-visible device MCP tools (DESIGN.md ADR-13, `app_tools.DEVICE_TOOLS`): `self.get_device_status`, `self.audio_speaker.set_volume`, `self.screen.set_brightness`, `self.screen.set_theme`, `self.screen.set_bedside`, `self.imu.get_state`; any other name is answered "Unknown device tool" and nothing is emitted. With the turn's reply open, the call emits a `device_action` event `{action_id, tool, args}` on the message stream and waits up to 8 s (`DEVICE_ACTION_WAIT_S`) for the gateway's `POST /chats/{id}/device_actions/{action_id}` `{ok, result?, error?}` (404 for an unknown or answered action); the model gets the device's result, its error, or "outcome unknown" after the wait. Without an open reply (the user is not in a conversation) the call is refused. The gateway (`claude_agent` provider, `docs/server.md`) enforces the same list before the device MCP channel.

## Steer, cancel, barge-in

- A message while a turn runs is a steer turn: at the next tool boundary its text follows the tool results and a separate note says the user spoke; the same loop continues (`interrupted{steered}`, not forced). A reply still streaming after `STEER_WAIT_S` (3 s) is cancelled and the steer runs on the rewritten tail. A held `ask_user` returns "No answer; the user's new message follows."
- `POST /chats/{id}/interrupt` and the stop lexicon cancel the stream (the HTTP response closes in ~1 ms), deny owned HA tokens and finish the task `cancelled`.
- The provider's stream dropping while reply text streams cancels the turn (`interrupted{barge_in}`); the partial reply is discarded and the next message gets `[note: your previous reply was cut off; the user heard: "…"]`. Without a message within 10 s a task-mode turn resumes as an event turn; a quick reply ends `cancelled{voice}`.

## Home Assistant

`HaGateway` (`ha_adapter.py`) implements ADR-9 (8 s boundary, dedupe and waiters, exact-match ownership, `/api/policy/approve|deny`, 2 s pending poll, boot reconciliation of provably-owned tokens) and serves the runner through `HaSession.call_tool`. `weixue_hamcp` holds one MCP session per process, opened lazily, ended with `DELETE` on shutdown, reopened once after a 404 (ha-mcp 8.6.0 is stateless: one `initialize` per process); `tools/list` is bounded to 20 s and refreshed every 15 min (5–300 s backoff after a failure) from a SQLite-cached copy, each refresh logging `tools= bytes= first_byte_ms= total_ms= session= sessions_opened= changed=`. The `e2e-guard` file in the data directory refuses free HA writes except `light.living_room_ceiling_lights`.

## Deployment

Compose service `agent-service` (`server/docker-compose.yml`): image `weixue/agent-service:local` (`python:3.12-slim` by digest, `requirements.lock` installed with `--no-deps` + `pip check`, uid 1000), `network_mode: ${WG_TUNNEL:+service:wg}`, environment `ANTHROPIC_API_KEY`, `HA_MCP_URL`, `AGENT_SERVICE_TOKEN`, `MONITOR_TOKEN`, `AGENT_DB=/data/db/agent.db`, `AGENT_HA_PROBE_S` (10) and `AGENT_HA_PROBE_TIMEOUT_S` (5), `TZ`, volume `./data/agent-service:/data` (SQLite WAL `db/agent.db`, the only directory agent-monitor mounts; `memory/` 0700), `mem_limit 512m` (RSS ~100 MiB), healthcheck `python -m weixue_agent.health` (DB writable, last model request not failed, ha-mcp ping within 60 s). Health probes (ha-mcp ping, endpoint TCP, tunnel, model API) run every `AGENT_HA_PROBE_S`; `/v1/status.probes` carries each result, a `health_probes` row is written when a probe's `ok` flips and otherwise every 300 s. No host port; the xiaozhi server gets `AGENT_SERVICE_URL` and `AGENT_SERVICE_TOKEN`. `scripts/setup.sh` generates `AGENT_SERVICE_TOKEN` once into `server/.env` (never printed), writes `AGENT_SERVICE_HOST=wg` while `WG_TUNNEL=1`, builds and recreates the service with the others and waits for its healthcheck after the voice-path checks; a first deployment that fails rolls back without it.

## Operation

- Recreate it with `scripts/setup.sh`, or together with `mcp-bridge` (`docker compose up -d mcp-bridge agent-service`): recreated alone, compose 5.3 attaches no network to a service whose `network_mode` interpolates to empty (`HostConfig.NetworkMode: none`). `docker restart agent-service` keeps its network.
- Logs: `docker logs agent-service` (JSON lines, secrets redacted; one line per request with model, effort, ttft, cost and cache tokens).
- Status: `docker exec agent-service python -m tools.sim_device status` (the token is in the container's environment).
- Simulator: `docker exec agent-service python -m tools.sim_device --url http://127.0.0.1:8010 ask "…"`; `watch` plays the gateway and device side of the outbox.
- Live gate: `server/agent-service/tools/e2e_live.sh [scenario…]` (scenarios in `tools/e2e_conversation.py`).
- Tests: `cd server/agent-service && python -m pytest` (fake streaming Messages API at the HTTP layer, fake ha-mcp); S1 measurements: `tools/spike_s1.py`.
