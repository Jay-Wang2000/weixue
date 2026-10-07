# feat-agent-service — developer brief (merged, rev 2)

Lead merge of architect A (job model, API, deliveries) and architect B (SDK, sessions, memory, HA), 2026-10-07; rev 2 reconciles it with the Codex Wave 5 M2 spec review. SDK facts cite the published wheel `claude-agent-sdk 0.2.164` (bundled CLI 2.1.292; a `manylinux_2_17_aarch64` wheel exists). Items marked UNVERIFIED are checked in the S0/S1 gate (section 13) before anything is built on them. Repo paths are relative to `~/Projects/weixue`.

Fixed by Jay: decisions 15–30 in `docs/DECISIONS-PENDING.md`, which win over this brief (numbers in parentheses below), plus Python `claude-agent-sdk` self-hosted in Docker, the personal API key and no claude.ai login, ha-mcp `wait_seconds` unchanged with timeouts in our layer (ADR-9). Section 14 lists what is still open. Never print secrets.

## 1. Goal & scope

Build `server/agent-service/`: a compose service that turns one device utterance into a durable **task**, drives the Claude Agent SDK, calls Home Assistant's ha-mcp directly through an in-process adapter that implements the ADR-9 pending/approval contract, and tells the device what happened through a tiered, durable outbox. The voice connection is only a window onto a task: closing it never cancels work.

In scope: container, process model, task/chat/interaction/delivery model, HTTP+SSE API with JSON Schemas, SDK session lifecycle, memory layout, HA adapter, error handling and deadlines (ADR-11), delivery tiers, config, observability, tests, and `tools/sim_device.py` that plays gateway and device.

## 2. Non-goals

- The xiaozhi provider `type: claude_agent` (feat-llm-claude-agent), the device `agent`/`chats` wire transport (feat-protocol-agent-msg), device UI (feat-ui-agent-states, feat-ui-sessions). This brief fixes the service side of each contract.
- `dispatch_dev_job`: separate future task `feat-dev-dispatch` (22).
- Chat deletion, transcript export, Ubuntu migration mechanics (the layout is migratable: fixed `cwd`, one data volume).
- Any change to ha-mcp policy or `wait_seconds`; applying `voice_policy.proposed.json` stays a user step.

## 3. Architecture & module layout

```
xiaozhi provider ──POST /v1/tasks (SSE)──► agent-service ──stdio──► claude CLI (one per live session)
gateway outbox   ◄──GET /v1/deliveries/stream──┤            └─ in-process MCP: ha (adapter), weixue (app tools)
device panel     ──POST /v1/interactions/{id}/…┤
                                               └──HTTP (HA_MCP_URL)──► ha-mcp (HA Green :9584, secret path)
                                                  office mode: from inside the wg namespace, like mcp-bridge
```

**Container** (`server/agent-service/Dockerfile`, `python:3.12-slim` pinned by digest in `vendor.lock`, pinned `requirements.txt` + `pip check`, non-root `agent` uid 1000). No Node: the wheel bundles the CLI. Paths: `/data/agent.db` (SQLite), `/data/claude` (`CLAUDE_CONFIG_DIR`: transcripts and auto-memory), `/workspace` (fixed SDK `cwd`, bind-mounted read-only from `server/agent-service/workspace/`; the encoded cwd is part of every session and memory path, so it never changes). Nothing under `/` or `/workspace`'s parents may hold a `CLAUDE.md`.

**Networking** (17, no relay). agent-service calls `HA_MCP_URL` itself with mcp-bridge's mode switch `network_mode: ${WG_TUNNEL:+service:wg}`: project network at home, the `wg` namespace in office mode, started only once the tunnel is ready and leaving only through `wg0` (fix-wave5-server C7). Others reach it at `AGENT_SERVICE_URL`, rendered by `setup.sh` as `http://agent-service:8010` or `http://wg:8010`. It publishes no host port (compose rejects `ports` with `network_mode: service:wg`): the simulator runs via `docker exec agent-service python -m tools.sim_device` and the monitor is its own container. `GET /healthz` needs a matching successful ha-mcp `ping` result within 60 s (an RPC error or a result-less 202 is unhealthy). When a `wg` restart removes its `eth0` it stops admitting tasks and exits non-zero within 10 s; `restart: unless-stopped` rejoins the new namespace and restart recovery (section 9) runs, so a tunnel toggle ends running tasks as `failed{service_restart}` with the WHEN_IDLE notice. `setup.sh` generates `AGENT_SERVICE_TOKEN` (`openssl rand -hex 32`, `server/.env` 0600, never printed).

**Process model.** One `uvicorn --workers 1` process, FastAPI + pydantic v2 (the schema source). One **SessionActor** per live CLI process owns its `ClaudeSDKClient`; everything else talks to it through an `asyncio.Queue` (`turn`, `inject`, `interrupt`, `close`). Each actor runs one continuous `receive_messages()` pump (not `receive_response()`: the read loop has a 100-message buffer that blocks control requests when nothing drains it) and closes the head of its turn FIFO on each `ResultMessage`. All CLI processes (chat, task, side turn) share one semaphore `CLI_PROCS_MAX` (default 4, re-set from S0 memory measurements; `mem_limit` set from the same numbers).

**Layout** (`server/agent-service/`): `weixue_agent/{__main__,config,logging,ids,schemas,store,api,sse,tasks,chats,runtime,sdk_options,routing,interactions,deliveries,recovery,weixue_tools,ha_adapter,health}.py`, `workspace/{CLAUDE.md,.claude/settings.json}`, `tools/{sim_device,e2e_long_task,export_schemas}.py`, `schemas/*.schema.json` (committed, drift fails a test), `tests/`. The bridge's `hamcp.py` and `config.py` move to a shared package `server/weixue_hamcp/` installed into both images; `voice_filter.py` stays in the bridge (legacy voice path only); bridge tests stay green.

## 4. Job model & state machine

| Term | Meaning |
|---|---|
| Device | `device_id` = MAC (xiaozhi `Device-Id`); sim uses `sim-device-01` |
| Chat | one SDK session (`sdk_session_id` uuid4 chosen at creation) + index row; a device has `current_chat_id` and `previous_chat_id` |
| Task | one user request (`POST /tasks`); outlives the connection; spans **parts** |
| Part | `main_turn` (a `query()` in the chat session), `task_session` (a forked SDK session running a background task), `followup_turn` (a turn we inject: task result, HA approval outcome, late answer), `side_turn` (section 7) |
| Interaction | pending `input` (AskUserQuestion) or `approval` (SDK gate or HA held call), `source ∈ sdk|ha_mcp` |
| Delivery | outbox row for the device: `agent` card, `chats` snapshot, spoken text (section 10) |

`task.state ∈ queued | progress | needs_input | needs_approval | done | failed | cancelled` (last three terminal). `task.mode ∈ foreground | background`, flips once. Terminal tasks carry `outcome ∈ ok|partial|denied|expired|unknown|none` and `side_effects[]` (`confirmed|failed|unknown|not_run`) built from `ToolUseBlock` / `ToolResultBlock(is_error)` pairs of non-read-only tools; a tool_use with no result at termination is `unknown`, and such a task is never reported as "cancelled cleanly" or "not done" (ADR-11).

```
queued ─start─► progress ─AskUserQuestion─► needs_input ─answered/expired─► progress
progress ─approval pending (sdk gate | HA held)─► needs_approval ─decided/expired─► progress
progress ─all parts finished, nothing pending─► done
progress|needs_* ─SDK error / refusal / BG_MAX_S / restart─► failed
any non-terminal ─cancel─► cancelled
```

**Background tasks are forked sessions, not SDK subagents.** The chat model calls `mcp__weixue__start_background_task(title, brief)`, which returns at once so the spoken reply stays one sentence; the service emits `backgrounded{reason:"delegated"}`. After that chat turn's `ResultMessage` (never mid-turn: a fork would copy a `tool_use` with no result) a task actor starts with `resume=<chat sid>, fork_session=True, session_id=<new uuid>`, prompt = `brief`. The task session has its own persisted id, survives the chat actor's idle drop, keeps `task_id` on every approval and question, and, being a main conversation, has `AskUserQuestion` and auto-memory (both unavailable inside `Agent`-tool subagents). Its final text is injected into the chat as a `followup_turn` and becomes the `done` delivery. The SDK's native background subagents are the S1 fallback only.

## 5. API (schemas)

Base `http://agent-service:8010/v1`; JSON UTF-8; RFC 3339 UTC ms; ids `tsk_|cht_|int_|dlv_` + 26-char ULID. Every route but `GET /healthz` needs `Authorization: Bearer <AGENT_SERVICE_TOKEN>`. Optional `X-Trace-Id` (32 hex) is generated when absent and echoed. Text caps follow the UI spec (title 32, summary 60, option 24, question 120); the service truncates with `…`, never rejects agent output. Errors: `{"error":{"code","message","detail"}}` with `invalid_request` 400, `unauthorized` 401, `not_found` 404, `idempotency_mismatch|already_decided|expired|invalid_state|stale_lease` 409, `too_many_tasks` 429 (+`Retry-After`), `unavailable` 503 (CLI cannot start, process cap, or key missing; the message never contains a value).

Idempotency: `POST /tasks` and `POST /chats` require `Idempotency-Key` (provider: `<device>:<xiaozhi session>:<turn seq>`; sim: uuid4; kept 24 h). Same key + same hash gives 200 with `duplicate:true` and, for SSE, replay from `Last-Event-ID`/`after` (not from 0); different hash gives 409. Decisions (`answer|approve|cancel`) carry a client `decision_id`: first wins, repeat gives 200 duplicate, conflict gives 409 `already_decided`, expired gives 409 `expired`.

Routes:

| Route | Purpose |
|---|---|
| `POST /tasks` (`Accept: text/event-stream` or json) | create or route an utterance; SSE body is the event stream with `until=foreground`, header `X-Task-Id` |
| `GET /tasks/{id}`, `GET /tasks?device_id&chat_id&state&limit` | Task, list |
| `GET /tasks/{id}/events?after=&until=terminal\|foreground` | SSE; `Last-Event-ID` overrides `after`; `: hb` every `HEARTBEAT_S`; ends with `event: end` |
| `POST /tasks/{id}/answer\|approve\|cancel\|release`; aliases `POST /interactions/{int_id}/answer\|approve`, `GET /interactions/{int_id}` | decisions (panel knows only `card_id = interaction_id`) |
| `GET /tasks/{id}/trace` | `{task, events, parts, tool_calls (args redacted), interactions, deliveries, cost_usd}` |
| `GET /chats?device_id&limit=12`, `POST /chats`, `POST /chats/{id}/switch`, `POST /chats/back`, `GET /chats/resolve?device_id&q`, `POST /chats/{id}/read` | chat index (feat-ui-sessions server half); switch prewarms the actor and never interrupts a running turn |
| `GET /deliveries/stream?device_id`, `POST /deliveries/{id}/ack`, `GET /devices/{id}/snapshot` | outbox (section 10) |
| `GET /healthz`, `GET /status` | health: DB writable **and** last CLI spawn succeeded **and** a successful ha-mcp `ping` within 60 s (section 3); status: `live_clients, queue_depth, open_tasks, pending_deliveries, last_api_error, sdk_version, cli_version` |

Normative schemas (`$id` prefix `https://weixue.local/agent/v1/`, written by `tools/export_schemas.py`):

```json
{"$defs": {
  "TaskId": {"type":"string","pattern":"^tsk_[0-9A-HJKMNP-TV-Z]{26}$"},
  "ChatId": {"type":"string","pattern":"^cht_[0-9A-HJKMNP-TV-Z]{26}$"},
  "InteractionId": {"type":"string","pattern":"^int_[0-9A-HJKMNP-TV-Z]{26}$"},
  "DeliveryId": {"type":"string","pattern":"^dlv_[0-9A-HJKMNP-TV-Z]{26}$"},
  "DeviceId": {"type":"string","minLength":1,"maxLength":64},
  "TraceId": {"type":"string","pattern":"^[0-9a-f]{32}$"},
  "Time": {"type":"string","format":"date-time"},
  "Lang": {"enum":["en","zh"]},
  "TaskState": {"enum":["queued","progress","needs_input","needs_approval","done","failed","cancelled"]},
  "Outcome": {"enum":["ok","partial","denied","expired","unknown","none"]},
  "Progress": {"type":"object","additionalProperties":false,"required":["phase"],
    "properties":{"step":{"type":["integer","null"],"minimum":0},"total":{"type":["integer","null"],"minimum":0},
      "label":{"type":"string","maxLength":40},"tool":{"type":["string","null"],"maxLength":128},
      "phase":{"enum":["thinking","tool","writing","retrying","waiting","slow"]}}},
  "SideEffect": {"type":"object","additionalProperties":false,"required":["tool","status"],
    "properties":{"tool":{"type":"string","maxLength":128},"target":{"type":["string","null"],"maxLength":128},
      "status":{"enum":["confirmed","failed","unknown","not_run"]}}},
  "Error": {"type":"object","additionalProperties":false,"required":["code","message"],
    "properties":{"code":{"enum":["service_restart","api_error","rate_limit","auth","billing","refusal","max_turns","max_budget","sdk_error","agent_process","timeout","busy","internal"]},
      "message":{"type":"string","maxLength":300}}},
  "Option": {"type":"object","additionalProperties":false,"required":["id","label"],
    "properties":{"id":{"type":"string","maxLength":32},"label":{"type":"string","maxLength":24}}},
  "Interaction": {"type":"object","additionalProperties":false,
    "required":["interaction_id","task_id","chat_id","kind","source","state","created_at"],
    "properties":{"interaction_id":{"$ref":"#/$defs/InteractionId"},"task_id":{"$ref":"#/$defs/TaskId"},"chat_id":{"$ref":"#/$defs/ChatId"},
      "kind":{"enum":["input","approval"]},"source":{"enum":["sdk","ha_mcp"]},
      "state":{"enum":["pending","answered","approved","denied","expired","cancelled","uncorrelated"]},
      "question":{"type":"object","additionalProperties":false,"required":["text"],
        "properties":{"text":{"type":"string","maxLength":120},"header":{"type":"string","maxLength":12},
          "options":{"type":"array","maxItems":4,"items":{"$ref":"#/$defs/Option"}},"multi_select":{"type":"boolean","default":false}}},
      "action":{"type":"object","additionalProperties":false,"required":["text","tool"],
        "properties":{"text":{"type":"string","maxLength":60},"detail":{"type":"string","maxLength":120},
          "tool":{"type":"string","maxLength":128},"risk":{"enum":["low","medium","high",null]}}},
      "voice_can_approve":{"type":"boolean"},
      "created_at":{"$ref":"#/$defs/Time"},"expires_at":{"anyOf":[{"$ref":"#/$defs/Time"},{"type":"null"}]},
      "voice_window_until":{"anyOf":[{"$ref":"#/$defs/Time"},{"type":"null"}]},
      "decision":{"type":["object","null"]},"via":{"enum":["panel","voice","api","timeout",null]}}}
}}
```

Exactly one of `question` (kind `input`) or `action` (kind `approval`) is present.

`POST /tasks` request:

```json
{"type":"object","additionalProperties":false,"required":["device_id","text"],
 "properties":{"device_id":{"$ref":"defs.schema.json#/$defs/DeviceId"},
  "chat_id":{"anyOf":[{"$ref":"defs.schema.json#/$defs/ChatId"},{"type":"null"}]},
  "text":{"type":"string","minLength":1,"maxLength":4000},
  "lang":{"enum":["en","zh","auto"],"default":"auto"},
  "deadline_at":{"$ref":"defs.schema.json#/$defs/Time"},
  "origin":{"type":"object","additionalProperties":false,"properties":{
    "kind":{"enum":["voice","panel","sim","api","retry"]},
    "gateway_session_id":{"type":"string","maxLength":128},"turn_seq":{"type":"integer","minimum":0}}},
  "retry_of":{"$ref":"defs.schema.json#/$defs/TaskId"}}}
```

`chat_id` null means the device's current chat (created if none). `lang: auto` is `zh` when the text contains CJK, else `en`.

Task:

```json
{"type":"object","additionalProperties":false,
 "required":["task_id","chat_id","device_id","state","mode","created_at","updated_at","foreground_deadline_at","last_seq","trace_id"],
 "properties":{"task_id":{"$ref":"defs.schema.json#/$defs/TaskId"},"chat_id":{"$ref":"defs.schema.json#/$defs/ChatId"},
  "device_id":{"$ref":"defs.schema.json#/$defs/DeviceId"},"state":{"$ref":"defs.schema.json#/$defs/TaskState"},
  "mode":{"enum":["foreground","background"]},"title":{"type":["string","null"],"maxLength":32},
  "summary":{"type":["string","null"],"maxLength":60},"lang":{"$ref":"defs.schema.json#/$defs/Lang"},
  "progress":{"anyOf":[{"$ref":"defs.schema.json#/$defs/Progress"},{"type":"null"}]},
  "pending":{"type":"array","items":{"$ref":"defs.schema.json#/$defs/Interaction"}},
  "outcome":{"anyOf":[{"$ref":"defs.schema.json#/$defs/Outcome"},{"type":"null"}]},
  "error":{"anyOf":[{"$ref":"defs.schema.json#/$defs/Error"},{"type":"null"}]},
  "side_effects":{"type":"array","items":{"$ref":"defs.schema.json#/$defs/SideEffect"}},
  "created_at":{"$ref":"defs.schema.json#/$defs/Time"},"updated_at":{"$ref":"defs.schema.json#/$defs/Time"},
  "foreground_deadline_at":{"$ref":"defs.schema.json#/$defs/Time"},
  "finished_at":{"anyOf":[{"$ref":"defs.schema.json#/$defs/Time"},{"type":"null"}]},
  "last_seq":{"type":"integer","minimum":0},"trace_id":{"$ref":"defs.schema.json#/$defs/TraceId"},
  "retry_of":{"anyOf":[{"$ref":"defs.schema.json#/$defs/TaskId"},{"type":"null"}]},
  "retry_allowed":{"type":"boolean"},"duplicate":{"type":"boolean"},
  "routed":{"enum":["new","answer","approval","side_turn"]}}}
```

Event envelope (`id: <seq>` / `event: <type>` / `data: <one line>`):

```json
{"type":"object","additionalProperties":false,
 "required":["v","seq","task_id","chat_id","trace_id","ts","type","data"],
 "properties":{"v":{"const":1},"seq":{"type":"integer","minimum":1},
  "task_id":{"$ref":"defs.schema.json#/$defs/TaskId"},"chat_id":{"$ref":"defs.schema.json#/$defs/ChatId"},
  "trace_id":{"$ref":"defs.schema.json#/$defs/TraceId"},"ts":{"$ref":"defs.schema.json#/$defs/Time"},
  "type":{"enum":["accepted","text","progress","backgrounded","foreground_closed","needs_input","needs_approval","resolved","done","failed","cancelled"]},
  "data":{"type":"object"}}}
```

| type | data |
|---|---|
| `accepted` | `{state, mode, routed}` first event of every stream |
| `text` | `{delta, part: "main_turn"\|"side_turn"\|"followup_turn"}`; only while `mode==foreground`; chat-model `text_delta` with `parent_tool_use_id is None`; persisted coalesced per 150 ms or sentence boundary, streamed live per delta |
| `progress` | `Progress`; at most one per 2 s per task except phase changes; `phase:"slow"` once when no text 8 s after `query()`, `phase:"retrying"` on `SystemMessage(subtype="api_retry")` |
| `backgrounded` | `{reason: "delegated"\|"deadline"\|"released", ack_text: string\|null}` once |
| `foreground_closed` | `{reason: "turn_done"\|"deadline"\|"released"\|"gateway_closed"}` once |
| `needs_input` / `needs_approval` | `{interaction: Interaction, speak}`; `speak` for approval e.g. "I need your OK on the panel to turn on the desk plug." |
| `resolved` | `{interaction_id, state, via}` |
| `done` | `{summary ≤60, speak: string\|null, outcome, side_effects}`; `speak` null when already spoken in the foreground |
| `failed` | `{error: Error, speak, outcome, side_effects}` |
| `cancelled` | `{reason: "user"\|"voice"\|"barge_in"\|"api", outcome, side_effects}` |

Decision bodies:

```json
{"answer":{"type":"object","additionalProperties":false,"required":["interaction_id","decision_id","via"],
  "properties":{"interaction_id":{"$ref":"defs.schema.json#/$defs/InteractionId"},"decision_id":{"type":"string","format":"uuid"},
   "option_ids":{"type":"array","minItems":1,"maxItems":4,"items":{"type":"string","maxLength":32}},
   "text":{"type":"string","minLength":1,"maxLength":500},"via":{"enum":["panel","voice","api"]}},
  "oneOf":[{"required":["option_ids"]},{"required":["text"]}]},
 "approve":{"type":"object","additionalProperties":false,"required":["interaction_id","decision_id","decision","via"],
  "properties":{"interaction_id":{"$ref":"defs.schema.json#/$defs/InteractionId"},"decision_id":{"type":"string","format":"uuid"},
   "decision":{"enum":["approve","deny"]},"via":{"enum":["panel","voice","api"]}}},
 "cancel":{"type":"object","additionalProperties":false,"required":["decision_id","reason"],
  "properties":{"decision_id":{"type":"string","format":"uuid"},"reason":{"enum":["user","voice","api"]}}},
 "release":{"type":"object","additionalProperties":false,"required":["interrupt","reason"],
  "properties":{"interrupt":{"type":"boolean"},"reason":{"enum":["barge_in","deadline","gateway_closed"]}}},
 "ack":{"type":"object","additionalProperties":false,"required":["lease_id","outcome"],
  "properties":{"lease_id":{"type":"string","format":"uuid"},"outcome":{"enum":["accepted","spoken","shown","deferred","dropped"]},
   "proactive_id":{"type":"string","maxLength":128},"ttl_s":{"type":"integer","minimum":10,"maximum":3600},
   "retry_after_ms":{"type":"integer","minimum":0},"detail":{"type":"string","maxLength":200}}}}
```

AskUserQuestion answers go back as `PermissionResultAllow(updated_input={"questions": <original>, "answers": {<question text>: <label | [labels] | free text>}})`; `option_ids` map to labels, free text that answers the pending question also goes under `answers`; `response` is used only for the off-topic case in section 7. Approve on `source:sdk` resolves the `can_use_tool` future (deny message "The user declined."); on `source:ha_mcp` it calls `ha_adapter.decide` (section 8). `release` is idempotent. Chat (`{chat_id, title, title_source, state: idle|working|needs_you|unread|failed, summary, unread, active_task_ids, last_activity_at, created_at}`), `GET /chats` (`{device_id, current_chat_id, previous_chat_id, items}`, current first), `switch` (`{current_chat_id, previous_chat_id, last_summary}`), `resolve` (`{matches:[{chat_id,title,score}]}`, top 3) and Delivery (`{delivery_id, gseq, lease_id, device_id, task_id, chat_id, kind: agent|chats, tier, slot, payload, speak, lang, attempt, expires_at, created_at}`) follow architect A's shapes unchanged; the `agent` payload uses the UI spec field names (`concept-cards.md` §10) with `progress→working`, `needs_input→needs_clarification` mapped only in `deliveries.py`.

## 6. SDK usage & sessions

`sdk_options.py` is the single `ClaudeAgentOptions` builder.

| Option | Chat session | Task session |
|---|---|---|
| `model` / `fallback_model` | `claude-fable-5-1` / `claude-opus-5-5` (decision 15) | same |
| `effort` | `low` | `high` |
| `thinking` | omit: Fable 5.1 thinking is always on, any other value is a 400 | omit |
| `max_turns` / `max_budget_usd` | 12 / 5.0 runaway guards | 60 / 25.0 |
| `cwd` | `/workspace` | same |
| `permission_mode` | `"default"` explicitly (omitted can start in `auto`) | same |
| `setting_sources` | `["project"]` (loads `/workspace/CLAUDE.md`, `.claude/settings.json`) | same |
| `system_prompt` | omit (minimal default); all rules live in the root `CLAUDE.md` | same |
| `tools` | `["Read","Write","Edit","AskUserQuestion","WebSearch","WebFetch"]` + MCP | plus `TaskCreate`,`TaskUpdate`,`TaskList`,`TaskGet` (CLI ≥2.1.268 ships these, not `TodoWrite`; UNVERIFIED that listing them is enough on Fable, else `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`) |
| `allowed_tools` | `["mcp__ha__*","mcp__weixue__*","WebSearch","WebFetch"]` (decision 22) | same |
| `disallowed_tools` | `["Bash","Glob","Grep","NotebookEdit","Agent","Skill"]` | same |
| `can_use_tool` | `interactions.gate`: AskUserQuestion → `needs_input`; Read/Write/Edit allowed only under the memory dir, else deny; other non-HA tools → `needs_approval` (`source:sdk`) | same |
| `hooks` | none in v1 | none |
| `mcp_servers` | `{"ha": {"type":"sdk","name":"ha","instance": ha_adapter.server, "disableAutoBackground": True, "timeout": 30000}, "weixue": create_sdk_mcp_server(...)}`; `strict_mcp_config=True` | same |
| `include_partial_messages` | `True` | `False` |
| `env` | `{"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS":"1","CLAUDE_AGENT_SDK_CLIENT_APP":"weixue-agent/1","AGENT_SERVICE_TOKEN":"","HA_MCP_URL":""}` (blank what the CLI must not inherit); `CLAUDE_CONFIG_DIR` is set only at container level (the SDK's session helpers read `os.environ`); `API_TIMEOUT_MS`/`CLAUDE_CODE_MAX_RETRIES` stay at SDK defaults (decision 16) | same |
| `stderr` | redacting logger at DEBUG | same |

Tool correlation comes from the stream, not hooks: `AssistantMessage.ToolUseBlock(id, name, input, parent_tool_use_id)` and `UserMessage.ToolResultBlock(tool_use_id, is_error)` fill `tool_calls`. Prompts are sent as dict messages through `client.query(async_iter)` with `origin={"kind":"human"}`; a `ResultMessage` whose `origin.kind=="human"` closes the FIFO head, anything else is logged as injected. `SystemMessage` subtypes `api_retry` (INFO + `progress{retrying}`), `model_fallback` and `model_refusal_no_fallback` (INFO) are routed. Costs: `ResultMessage.model_usage` totals are per CLI process and cumulative; keep a per-process baseline reset on connect and on `ConversationResetMessage`, and attribute each result to its task through the FIFO.

**Lifecycle.** Connect flag is decided from disk, not memory: `resume=<sid>` when `$CLAUDE_CONFIG_DIR/projects/-workspace/<sid>.jsonl` exists, else `session_id=<sid>`; a `ProcessError` on resume retries once with `session_id`. Never `continue_conversation` (all chats share one cwd). A switch prewarms with `connect()` and no prompt; the device UI is served from the index, so the 1 s target never waits on the CLI. Idle disconnect after `CLIENT_IDLE_S=300` with nothing running or pending; LRU eviction under the process cap evicts idle actors only. Back-pressure order: evict idle → queue background tasks as `queued` → a voice main turn waits `QUEUE_WAIT_MS` then fails `busy` with 503 and a spoken "I'm busy right now, try again in a moment" (zh 「我这会儿忙，稍后再试」). `CanUseToolShadowedWarning` at connect is expected (HA and web tools are pre-allowed) and logged once.

**Index** (`store.py`, SQLite, `user_version` migrations): `devices`, `chats` (`chat_id, device_id, sdk_session_id UNIQUE, title, title_source, summary, created_at, last_activity_at, last_read_seq`), `tasks` (A's columns plus `retry_allowed`), `task_parts` (`kind, sdk_session_id, state, started_at, ended_at`), `tool_calls`, `task_events` (coalesced text, pruned after `EVENT_RETENTION_DAYS` = 30, decision 21), `interactions` (`external_ref` = ha-mcp token, never returned by the API), `approvals` (section 8), `deliveries` (`lease_id, attempt, proactive_id`), `idempotency`, `ha_tools_cache`. Title: placeholder → first utterance (32 chars) → `set_chat_title`; `SDKSessionInfo.custom_title`/`first_prompt` are read separately and `rename_session` is not used (it would make `summary` echo our own title). WAL is the default; S0 tests WAL on the actual Rancher bind mount (open, `kill -9`, reopen, `integrity_check`, write as uid 1000) and falls back to a named volume or `journal_mode=DELETE, synchronous=FULL`.

## 7. Routing of new utterances and concurrency

`POST /tasks` for device D, chat C:

1. **Spoken interaction window**: `devices.last_spoken_interaction_id` (set from the provider's inline speak or the proactive `spoken` ack) is checked first, across chats, within `VOICE_WINDOW_S=20`. For an `input` the text becomes the answer (`routed:"answer"`). For an `approval` deny words count (en "no / deny / cancel / stop", zh 「不 / 不要 / 拒绝 / 取消」, exact match, punctuation ignored). Voice approval waits on Q1: until then `voice_can_approve:false` and approving needs a panel decision carrying `card_id`. Other text falls through.
2. Outside the window, pending interactions take panel answers only; a late panel answer to a question whose turn already ended is injected as a `followup_turn`.
3. **Chat idle** (task sessions may be running): new `main_turn`. This is how "what's the weather" is answered during a long task.
4. **Chat busy**: `queued` for `QUEUE_WAIT_MS=1500`, then a `side_turn` on a short-lived fork (`resume=C, fork_session=True`), recorded on the task and prepended to the chat's next main turn as one line `[While you were busy the user asked: "…"; you answered: "…"]`. One chat session never runs two `query()` calls at once, and the provider returns as soon as the foreground closes, so xiaozhi never runs two `chat()` calls for us.

An AskUserQuestion still pending in a main turn at `foreground_closed` is denied with "The user has not answered yet; end the turn. Their answer will arrive as a new message", which frees the chat session; the card stays. A chat switch never interrupts anything; running tasks keep their own `chat_id` on deliveries.

## 8. HA adapter & approvals (ADR-9 inside this service)

**Exposure** (19): in-process `McpSdkServerConfig{"type":"sdk","name":"ha","instance": <low-level mcp.server.Server>}`, one instance per actor bound to `(chat_id, task_id)`, backed by one process-wide `HaGateway` (direct streamable-HTTP client to `HA_MCP_URL` from `weixue_hamcp`, approvals table, pending poller). This adapter is the 8 s boundary (Q4). `list_tools` answers from `ha_tools_cache` with **every** ha-mcp tool, unfiltered; the cache refreshes in the background and a change calls `client.reconnect_mcp_server("ha")` on live actors (UNVERIFIED method name). Not the SDK's `type:"http"` server: no 8 s boundary, and the secret URL would sit in the CLI's MCP config. No `PreToolUse` `defer`: it would stop the call before ha-mcp sees it (a second gate).

**Call path** (`HA_CALL_BUDGET_S=8`, HTTP read timeout = `wait_seconds + 20` read from `/api/policy/config` at start; `approval_ttl_minutes` and `wait_seconds` are never hardcoded):

1. Dedupe key `(tool, sha256(canonical args))`. An identical call of ours in flight or pending adds this `(task_id, chat_id, tool_use_id)` to that approval's **waiters** and returns its `PENDING_APPROVAL` text; no second request is sent.
2. Persist `approvals` row `state=in_flight` (`approval_id, device_id, tool, args_json, args_hash, created_at, waiters_json`) before sending.
3. `asyncio.create_task(ha.rpc(call))` and `wait(timeout=8.0)`. Done in budget: return the result with ha-mcp's `isError` passed through (the SDK gives `is_error` to Claude).
4. Else `state=pending`, return at once: `PENDING_APPROVAL id=<approval_id>: Home Assistant is holding <tool> <domain>.<service> <entity> for approval on the weixue panel. Do not call it again; the outcome will arrive as a follow-up message.` Emit `needs_approval` (card text from tool + args). The original request keeps waiting up to `wait_seconds`. Pending proves only that the result is late, not that HA holds the call.
5. **Correlation**: poll `GET /api/policy/pending` every 2 s while any approval is pending (once per minute otherwise). A row is **owned** only on exact identity: tool plus untruncated canonical args equal to ours (or a request id if the installed version has one), within ±10 s of `created_at`, exactly one candidate. Then `ha_token` (stored, never leaves the service) and `expires_at` fill the card. Truncated args, several candidates or none: `state=uncorrelated`, a read-only card ("approve in the Home Assistant panel"), no device decision; another client's identical call is never adopted.
6. **Decisions** from the device go through `POST /api/policy/approve|deny {token}` (existing secret path) only for owned rows whose `device_id` matches; duplicates are idempotent per `approval_id`.

| Event | State |
|---|---|
| Original returns success | `executed` |
| Original returns `USER_DENIED` | `denied` |
| Original returns `USER_APPROVAL_REQUIRED` (wait elapsed) | `awaiting_decision_after_wait` |
| Then our own `/approve` call returns success (positive evidence; leaving `/pending` is no receipt) | persist `replay_dispatched`, then one `replay` of the identical call → `executed` or `failed`; a lost approve response is `unknown` |
| TTL passes, or `/approve` returns 404 | `expired` |
| Transport error or service restart | `unknown`; never replayed (ADR-9: no blind retry) |

Terminal transitions close every waiter and inject one `followup_turn` per chat: `[event approval <id>] <tool> <summary>: executed | denied | expired | failed: <text> | result unknown. Already handled; do not call the tool again.` Sent with `client_composed: True` (HA text never triggers `@path` expansion); because such turns skip nested attachments, the rules they need ("never retry an unknown result") live in the root `CLAUDE.md`.

**Cancel** denies an owned token through the same route; it is not optional. Uncorrelated holds keep a card reading "may still be waiting in the Home Assistant panel", never "cancelled". **Boot** reconciliation (Q5) lists pending and denies only tokens whose ownership is provable (our `approvals` row holds that `ha_token`); their tasks stay `unknown` until the deny result arrives. Other pending calls are left alone and shown on the monitor. An HA restart drops ha-mcp's queue: a later decision gets `unknown_token`, mapped to `expired`.

**Gate model** (decision 20): HA tools are in `allowed_tools`, so ha-mcp policy alone decides held versus free and the panel approves held calls; `can_use_tool` covers only AskUserQuestion, the memory-dir path guard and any future non-HA tool (web tools are pre-allowed). If S0/S1 item 4 finds an exposed tool that can edit the policy or approve, it goes to Jay as a question, not into an application filter. One device approval queue, two sources, each call exactly one source.

## 9. Error handling & deadlines (ADR-11)

| Event | Rule |
|---|---|
| Task created | `foreground_deadline_at = min(body.deadline_at, created_at + 90 s)`; the provider sends STT-end + 90 s |
| Deadline passes in foreground | emit `backgrounded{deadline, ack_text}` (en "This is taking a while. I'll tell you when it's done." / zh 「这个要花点时间，好了我告诉你。」) and `foreground_closed{deadline}`; later text never reaches the voice stream and the final text becomes a WHEN_IDLE `done` delivery. Both the service and the provider enforce the 90 s on their own clocks (the RCA showed SDK timeouts cannot bound a turn). action scheduling after the deadline: Q2; a dispatched action is never reported as cancelled |
| SSE client disconnects with no prior `release` | treated as `release{interrupt:false, reason:"gateway_closed"}` so the result is still delivered. The provider first retries `GET /events?after=<last_seq>&until=foreground` once within its deadline, then speaks the "can't reach" cue |
| `release{interrupt:true}` (barge-in) | if the chat main turn is still foreground, `client.interrupt()` on the **chat** actor only (task sessions are separate processes and keep running: interrupt ≠ cancel, P3); the task is `cancelled{barge_in}` with outcome from side effects; partial text stays in the transcript and is never spoken; the next prompt in that chat is prefixed `[note: your previous reply was cut off and not heard]` |
| `POST /cancel` | deny pending interactions (`PermissionResultDeny(interrupt=True)`, HA deny through `/api/policy/deny` for owned tokens), `interrupt()` running turns, `disconnect()` the task actor; wait `CANCEL_GRACE_S=10` for `terminal_reason in (aborted_streaming, aborted_tools)`, then force-disconnect; emit `cancelled{outcome, side_effects}`; a call already sent to HA is "may have happened", never undone |
| `BG_MAX_S=1800` after `backgrounded` | interrupt + disconnect the task actor, `failed{timeout}` |
| `AssistantMessage.error` / `ResultMessage.is_error`, `api_error_status` | `failed{code}` with `rate_limit`, `auth`, `billing`, `api_error`, `max_turns`, `max_budget`, `sdk_error`; spoken cue chosen per code; the stream is closed with a terminal event in every case |
| Fable `refusal` stop | `failed{refusal}`, speak "Sorry, I can't help with that one." (whether `fallback_model` covers refusals is observed in S1 through the `model_fallback` subtypes) |
| `CLIConnectionError` / `ProcessError` | `failed{agent_process}`; the actor disconnects and the next command reconnects with `resume` |
| Restart (`recovery.py`, before the API opens) | `queued` → `failed{service_restart}` silent; `progress|needs_*` → `failed{service_restart}`, outcome `unknown` if any non-read-only tool call has no end, else `none`; interactions `expired` except HA rows (section 8); background tasks get a WHEN_IDLE notice "I restarted while working on <title>, so it may not have finished" with a `Retry` option **only when `retry_allowed`** (no unknown side effect), otherwise a "check state" read-only follow-up; `CLAUDE_CODE_RESUME_INTERRUPTED_TURN` is never set; deliveries keep state, expired leases return to `pending`; chats unchanged |

Nothing is retried automatically after an unknown outcome. A late result never writes a new spoken turn: the SDK owns history, and the service only decides what the device hears.

## 10. Delivery tiers & device queue

The service owns the durable outbox (tier, supersession, expiry, attempts); the gateway (feat-protocol-agent-msg) owns transport and device state. One consumer per `device_id`: a new `GET /deliveries/stream` revokes the previous one; every offer carries a `lease_id` the ack must echo (stale → 409 `stale_lease`). Offers are ordered INTERRUPT, WHEN_IDLE, SILENT, then `gseq`.

**Spoken deliveries** ride the proactive queue (`docs/proactive.md`) in two phases. The gateway posts `POST /weixue/proactive/messages` with `Idempotency-Key=<delivery_id>:<attempt>`, `device=<device_id>` always set, `urgent=(tier==INTERRUPT)`, `ttl=min(3600, remaining)` and acks `accepted{proactive_id, ttl_s}`; the delivery becomes `handed_off` with `lease_until=min(expires_at, now+ttl_s)`. The gateway then polls `GET /weixue/proactive/messages/{id}` and acks `spoken` (→ `delivered`), or `deferred` on `expired|failed` (→ `pending`, next attempt, new key), proactive 429 → `deferred` with `Retry-After`. A xiaozhi restart drops its in-memory queue: `handed_off` leases expire and the delivery re-pends. Speech stays valid until `expires_at` (24 h for `done`), re-posted at most hourly.

| Source | Foreground open | After `backgrounded` | expires_at | Slot |
|---|---|---|---|---|
| `progress` | none (stream carries it) | SILENT `agent{working}` | none | `task:<id>`, newest replaces pending |
| `needs_input` | spoken inline; card SILENT | WHEN_IDLE question + card | `INPUT_TTL_S=1800` | `int:<id>` |
| `needs_approval` | spoken inline; card SILENT | WHEN_IDLE cue + card (INTERRUPT if the agent marked it urgent) | interaction `expires_at` | `int:<id>`, withdrawn on decision |
| `done` / `failed` | inline; card SILENT `unread` | WHEN_IDLE one sentence + card | 24 h speech, card stays | `task:<id>` |
| `cancelled` | — | SILENT | none | `task:<id>` |
| `notify_user(urgent=true)` | — | INTERRUPT | 1 h | own |
| chat index change | SILENT `chats` | SILENT `chats` | none | `chats`, coalesced |

INTERRUPT = proactive `urgent` (ignores mute; reserved for agent-declared urgency and HA safety); WHEN_IDLE waits for quiet, mute and TTL (acceptance: delivered within 10 s of returning to idle); SILENT is cards only. A spoken delivery superseded before it is offered is dropped. Device `card_shown`/`task_open` → `POST /chats/{id}/read` + ack `shown`. An idle device has no socket, so `GET /devices/{id}/snapshot` serves every pending card when a session opens.

## 11. Config & secrets

`server/.env` (0600) is the single key source (decision 16): `ANTHROPIC_API_KEY` (the personal key the current provider uses), `HA_MCP_URL` (already there for the bridge), `AGENT_SERVICE_TOKEN`. Compose resolves `env_file` and `environment:` alike, so `docker compose config` shows them and never runs in gates or logs. agent-service gets a service-specific `environment:` with only those keys plus `TZ` and `CLAUDE_CONFIG_DIR=/data/claude`, never `env_file: .env` (every server secret); the CLI keeps only the API key (section 6 `env`). `AGENT_SERVICE_TOKEN` reaches xiaozhi as an env var like `WEIXUE_PROACTIVE_TOKEN`, never in `data/.config.yaml`. Constants (`config.py`): `FOREGROUND_MAX_MS 90000`, `HEARTBEAT_S 2` while foreground else 5, `QUEUE_WAIT_MS 1500`, `VOICE_WINDOW_S 20`, `INPUT_TTL_S 1800`, `BG_MAX_S 1800`, `CANCEL_GRACE_S 10`, `CLI_PROCS_MAX 4`, `CLIENT_IDLE_S 300`, `MAX_OPEN_TASKS_PER_DEVICE 20`, `HA_CALL_BUDGET_S 8`, `HA_PENDING_POLL_S 2`, `EVENT_RETENTION_DAYS 30`. Error messages never echo values.

Compose: `agent-service` with `network_mode: ${WG_TUNNEL:+service:wg}`, no `ports`, the `environment:` above, volumes `./data/agent-service:/data`, `./agent-service/workspace:/workspace:ro`, `mem_limit` from S0, `restart: unless-stopped`, healthcheck `python -m weixue_agent.health` (15 s, start period 30 s). `mcp-bridge` is unchanged.

**Memory layout.** In git, read-only at `/workspace`: `CLAUDE.md` (household identity, spoken style from `weixue-agent-prompt.txt`: language of the turn, one or two sentences, no Markdown; device state only from HA; "work over ~20 s goes to `start_background_task`"; never retry an unknown result; how to read `PENDING_APPROVAL` / `[event approval …]`), `.claude/settings.json` (allow `Read|Edit|Write(//data/claude/projects/-workspace/memory/**)`, deny rules mirroring `disallowed_tools`; exact rule syntax UNVERIFIED, S1). Not in git, volume at `/data/claude` (0700): transcripts `projects/-workspace/<sid>.jsonl`, auto-memory `projects/-workspace/memory/`, the CLI's `.claude.json`. Auto-memory stays on (Jay-fixed mechanism; `autoMemoryEnabled` set explicitly if the `settings` option exposes it, UNVERIFIED). Per-turn context (Dublin local time, device id, bedside/muted) is prefixed to the user text, not the system prompt, so the cache prefix stays stable; `verbatim_prompts` stays off.

## 12. Observability

JSON lines to stdout (`logging.py`): `ts, level, msg, trace_id, task_id, chat_id, device_id, part, sdk_session_id, event, tool, outcome, latency_ms, model, cost_usd, attempt, code`; one line per state change, part start/end, tool call, interaction open/decide, delivery offer/ack, `api_retry`, `model_fallback`, SDK error, connect/resume/disconnect (with resume latency), recovery summary. Text deltas are never logged; summaries truncated to 60 chars. Redaction: never the API key, HA URLs, tokens, `private_`/`token=` URLs, ha-mcp tokens; tool args logged as `args_redacted_json` (keys matching `/(token|pin|password|secret|key|url)/i` → `[redacted]`, strings capped at 120); SDK stderr through the same redactor. Trace id travels provider → service log lines, so one grep across `xiaozhi-esp32-server` and `agent-service` logs reconstructs a turn. Per task: TTFT, time to `backgrounded`, time to terminal, `cost_usd`, in `/tasks/{id}/trace` and the terminal log line. Gate logs saved redacted under `docs/logs/agent-service-<scenario>-<date>.log`.

## 13. Test plan & e2e gate

**S0/S1 gate checklist** (first gate, from the Codex Wave 5 review; evidence per item in the spec; an office-mode deferral is outstanding, not passed):

1. The exact pinned SDK/CLI on arm64 with the personal key, the approved models, fallback and the retention setting (Q7); record the actual option and event shapes.
2. Session creation, restart/resume, safe forks, interrupt → drain → new-turn isolation, concurrent memory writes.
3. HTTP approvals against the installed ha-mcp version: early/late approve, deny, expiry, concurrent identical calls from another client, truncated-argument collisions, lost responses, restart of either service; `wait_seconds` untouched.
4. The complete HA tool surface against the effective policy, including indirect dispatch and policy/approval-management tools.
5. Fault tests of namespace replacement and deployment rollback; outbox partial delivery and crash after speech before ack.
6. The physical audio gate on the final stack (home): corrections during playback, all three cues followed by short answers, abort during cue loading, proactive failures, one real HA action.

**Spike details.** S0: `pip install claude-agent-sdk==0.2.164` in the arm64 image; CLI runs as uid 1000 with `CLAUDE_CONFIG_DIR=/data/claude` and `ANTHROPIC_API_KEY` only; WAL on the real bind mount; RSS of one idle and one working CLI (sets `CLI_PROCS_MAX`, `mem_limit`). S1 with `ClaudeSDKClient`: (a) `session_id` then `resume` after prewarm-only `connect()` and after restart; (b) `fork_session=True` resume after the parent's result, and a side-turn fork while the parent is mid-turn; (c) AskUserQuestion in a forked task session reaches `can_use_tool`, answer shape accepted; (d) Task* tools appear with the `tools` list on Fable, `tool_use_result` carries ids; (e) `api_retry`, `model_fallback`, `model_refusal_no_fallback` subtype names; (f) cold `connect()` to first token with HA up and down (cached `list_tools`); (g) memory: "remember my codeword" → restart → new chat recalls it; 4 concurrent clients writing memory plus a restart leave `MEMORY.md` and `.claude.json` intact, else serialize through an `mcp__weixue__remember` tool or ask Jay about `CLAUDE_CODE_DISABLE_AUTO_MEMORY`; (h) `interrupt()` on a chat actor leaves a running task actor untouched; (i) `max_budget_usd` accumulation across `query()` calls; (j) settings.json path-rule syntax; (k) whether ha-mcp still executes an approved held call after the client's HTTP request dropped (decides `unknown` vs `executed` at boot). Fallback if (b)-(c) fail: SDK background subagents with `TaskUpdatedMessage`/`TaskNotificationMessage` terminal statuses (`TERMINAL_TASK_STATUSES`) closing parts; API and routing unchanged.

**Unit** (`tests/`, no network; `FakeSDKClient` factory replaying scripted messages, `FakeTransport` for stream-json fixtures recorded once live, fake ha-mcp as an httpx `MockTransport` plus a Starlette streamable-HTTP app with hold durations, progress every 15 s, `USER_APPROVAL_REQUIRED` after wait, `USER_DENIED`, consume-on-recall, `/api/policy/pending|config|approve|deny`): `test_task_state` (every transition, outcome rules), `test_idempotency` (keys, decision_id, replay from `Last-Event-ID`), `test_sse` (framing, subscribe-before-replay with no gap or duplicate, heartbeats, `until=foreground`), `test_deadline` (speech closes at 90 s, canned ack per language, later text never on the stream, WHEN_IDLE done; post-deadline scheduling per Q2), `test_background` (fork only after result, result injected as follow-up, new main turn during a task, `BG_MAX_S`), `test_routing` (cross-chat spoken window, deny-only lexicon, "yes" during an HA card never approves, busy → side turn), `test_interactions` (answers shape, TTL deny, foreground_closed denial frees the session, late panel answer as follow-up), `test_cancel` (interrupt scope, grace, HA deny sent, uncorrelated wording), `test_recovery` (each non-terminal state, `retry_allowed`, HA rows reconciled, deliveries re-pended, chats untouched), `test_chats` (create/switch/back/resolve/read, title lifecycle, no interrupt on switch), `test_deliveries` (tier table, slots, two-phase ack, lease ownership, proactive 429, expiry, name mapping), `test_ha_adapter` (free call ≤8 s; early approval; approval after pending before wait → executed; after wait → replay only after our approve call succeeded and `replay_dispatched` is persisted; truncated, duplicate or foreign candidates stay uncorrelated; boot denies owned tokens only; deny; TTL; duplicate click; identical second call deduped with both waiters closed; two chats one decision both terminal; uncorrelated; poll catches a missed request; HA restart → `unknown_token` → expired; service restart mid-pending → unknown, no replay; `list_tools` served from cache with HA down), `test_backpressure` (cap, eviction order, 503 busy), `test_schemas` (fixtures validate, export equals committed files), `test_redaction` (synthetic secrets never appear in logs, trace, API errors, MCP config, transcripts or monitor rows).

**Live SDK** (`RUN_LIVE_TESTS=1`, key sourced from `server/.env` in a subshell): short question with TTFT; codeword across disconnect/resume and across container recreate; interrupt gives `aborted_*`; S1 assertions.

**End-to-end gate (office mode).** Simulated device (`tools/sim_device.py` and later `server/scripts/proactive_sim_device.py`), no audio, live HA over the WireGuard tunnel, read-only or test entities only, logs redacted under `docs/logs/`:

1. `chats new`; `ask` "Look at the living room lights and today's sunset time, check their history this week, and draft (don't apply) an automation that turns them on at sunset." Expect ≥3 HA tool calls in a task session, `backgrounded{delegated}` and `foreground_closed` inside 90 s, total over 30 s.
2. While it runs, `ask` "What's the weather like?" in the same chat (WebSearch or the HA weather entity): `routed:"new"`, a main turn, `done` inline; logs show no two main turns in flight for the chat.
3. The first task's `done` arrives on `watch` as WHEN_IDLE; with `--busy` the first offer is deferred and redelivery lands within 10 s of un-busying.
4. "Remind me about the bins" → AskUserQuestion "Which day?" → `answer --text Thursday` → `done`.
5. Cancel a running task: `cancelled` within 10 s with `side_effects`.
6. `docker restart agent-service` during a task: `failed{service_restart}`, WHEN_IDLE notice, `chats list` identical.
7. Five chats, ten switches, "what did we talk about?" lands in the right chat each time; restart keeps list and current chat.
8. One free HA action through the adapter on the test entity and window chosen in Q8 (outstanding until answered): on, then off; `ha_get_state` confirms; `side_effects: confirmed`; initial state restored. The held-call path runs against the fake ha-mcp unless Jay has applied the policy overlay.
9. A 5-min voiceless task survives a simulated device disconnect and its result arrives via WHEN_IDLE.

Per CLAUDE.md the spec must state that this task cannot be exercised on the device end to end; feat-llm-claude-agent's gate covers wake → STT → agent → TTS, and feat-protocol-agent-msg / feat-ui-agent-states cover the cards.

## 14. Open questions for Jay

Decisions 15–30 answered rev 1's questions. Resolved by the orchestrator: **Q4** an in-process adapter implements the 8 s boundary and calls ha-mcp over HTTP directly; **Q5** startup denies earlier pending calls only when ownership is provable (our correlation record), others stay and show on the monitor (section 8). Open:

- **Q1 Voice approval.** Decision 20: voice denies or cancels, never approves in v1; decision 25: explicit phrases, repeat-back for a bare "yes", irreversible/money tap-only. Options: (a) 20 holds; (b) 25 supersedes it, naming the tap-only actions and how a phrase identifies its card.
- **Q2 90 s deadline.** Options: (a) stop scheduling new actions (ADR-11); (b) continue as background work, result via WHEN_IDLE (rev 1). Both close speech at 90 s.
- **Q3 Overnight card vs HA token.** Decision 24 keeps bedside approvals to 07:30; the ha-mcp token expires earlier and a card cannot extend it. Options: (a) the card expires with the token; (b) it stays and a tap issues a fresh, separately approved request.
- **Q6 Ambiguous delivery ack after a restart.** Options: (a) replay, accepting occasional duplicate speech; (b) no replay, an "uncertain delivery" card.
- **Q7 Retention.** Is the 30-day retention `claude-fable-5-1` needs confirmed on the personal API org (15)? Needed for S0 item 1.
- **Q8 Test entity and window** for the deferred HA action and device-audio gates.

## 15. Follow-ups for the dependent tasks

- **feat-llm-claude-agent**: sync `httpx` SSE client to `POST /v1/tasks` (connect 2 s, read 15 s); `Idempotency-Key <device>:<session>:<turn>`, `X-Trace-Id`, `deadline_at` = STT end + 90 s; yield `text`, `speak` of `needs_*`/`backgrounded`/`failed`; end on `foreground_closed` or a terminal event; check `conn.client_abort` after every SSE line including `: hb` and send `release{interrupt:true}`; own 90 s monotonic deadline → cue + `release{deadline}`; on stream drop try `GET /events?after=` once, then the "can't reach my assistant" cue and no history write; device MCP tools (`self.*`) are out of this contract; `AGENT_SERVICE_TOKEN` via env; retire the sentence_id guard only once the provider path is live, when voice HA calls move here and the bridge's legacy behaviour retires.
- **feat-protocol-agent-msg**: consume `GET /v1/deliveries/stream` with the two-phase ack and `lease_id`; `agent`/`chats` as `custom`; snapshot on session open; panel events `approval_decision/clarification_choice/card_later/card_shown/task_open/task_stop/chat_switch/chat_new` mapped to the interaction, chat and read routes; decide whether the long-poll signals "state changed" without opening a voice session.
- **feat-ui-sessions**: list from `GET /chats` (current first, ≤12), `switch`/`back`/`resolve` (≥2 matches above 0.5 → clarification), `last_summary` for the switch toast, `read` on open; voice synonyms resolved in the provider; chats sit in the single management view (27).
- **feat-approvals**: its approval source becomes this service's interactions plus ha-mcp policy; the held-call live test needs the policy overlay applied by Jay.
- **fix-wave3-server / proactive**: keep `GET /weixue/proactive/messages/{id}` polling and `device` mandatory on agent posts.

## Addendum 2026-10-07: data the monitor board needs (feat-agent-monitor)

The agent service owns the single SQLite database; the monitor reads it read-only. Persist, with timestamps and ids that join to chats/tasks: every tool call (name, args summary, result summary/status, duration, ha-mcp token id when held, approval decision + source device/panel/voice + decided_at), every SDK result's usage (input/output/cache tokens, cost estimate, model, session kind), task state transitions, device connection events (WS open/close, device id), health probe results (ha-mcp reachable, tool count, pending count, policy version; mcp-endpoint; bridge; tunnel handshake age), and errors. Keep a daily rollup table for tokens/cost. Retention follows decision 21. The board binds 127.0.0.1 only and is reached over Tailscale once the backend runs on the home server (decision 30).

## Review risks carried into implementation (Codex Wave 5)

- A failed SDK `resume` must not silently become a fresh session under the same chat id: surface the error, keep the history association, let the user choose.
- `can_use_tool` is not a filesystem guard: enforce the memory-dir boundary with permission rules plus filesystem isolation (dedicated cwd, no symlink escape); test traversal.
- `interrupt()` must drain the old turn (buffered partial messages stay attached to their generation) before the next query is admitted; already-dispatched HA work is reported as outcome-unknown, never retried.
- Outbox: define `partial` and lost-record/404 handling; a handed-off question cannot be withdrawn through the proactive API, so supersession marks it stale on arrival instead; INTERRUPT deliveries bypass the quiescence wait.

## Answers 2026-10-07 (override §14 where they conflict)

- Q1: explicit phrases approve reversible actions; irreversible/money tap-only; one card at a time; repeat-back on a bare yes. Implement voice approval accordingly (not deny-only).
- Q3: card expires with the ha-mcp token; the agent acknowledges the lapse; no renewal path.
- Q7: defaults; S0 verifies Fable; retention 400 → fallback model + monitor entry.
- Q8: test entity `light.living_room_ceiling_lights` (restore state after each test).
- Q2, Q6: pending Jay (plain-language explanation requested).
