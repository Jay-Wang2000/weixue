# feat-agent-service — developer brief (rev 3)

Rev 3 (2026-10-07) replaces rev 2's forked-task model and 90 s deadline with decision 36 and the rewritten ADR-11; rev 2's other contracts stay. SDK facts cite the wheel `claude-agent-sdk 0.2.164` (bundled CLI 2.1.292, arm64 wheel exists) and its source: `interrupt()` sends an `interrupt` control request (60 s default control timeout); an interrupted turn's `ResultMessage.terminal_reason` is `aborted_streaming|aborted_tools`; `ResultMessage.origin` ties a result to its prompt. UNVERIFIED items are checked in the S0/S1 gate (section 13) first. Paths relative to `~/Projects/weixue`.

Fixed by Jay: decisions 15–36 in `docs/DECISIONS-PENDING.md`, which win over this brief (numbers in parentheses), plus self-hosted Python `claude-agent-sdk` in Docker, the personal API key (no claude.ai login), ha-mcp `wait_seconds` unchanged (ADR-9). Open items: section 14. Never print secrets.

Jay's model: "I call Jarvis, 'do something for me'. It acknowledges; the exchange drops after 8 s of no voice. It keeps working and reports back when done. Same Claude session. While waiting I can wake it again and ask for progress or redirect, just like interrupting Claude Code."

## 1. Goal & scope

Build `server/agent-service/`: a compose service that keeps one Claude Agent SDK session per chat, runs each user message as a turn that may outlive its voice exchange, lets a new message interrupt and steer the turn in flight, calls ha-mcp directly through an in-process adapter implementing the ADR-9 pending/approval contract, and reports results through a tiered, durable outbox. Closing the voice connection never cancels work.

In scope: container, process and job model, HTTP+SSE API with JSON Schemas, SDK sessions and interrupts, memory, HA adapter, errors (ADR-11), delivery tiers, config, observability, tests, and `tools/sim_device.py` (gateway + device).

## 2. Non-goals

- The xiaozhi provider (feat-llm-claude-agent), device transport and playback/user acks (feat-protocol-agent-msg), device UI (feat-ui-agent-states, feat-ui-sessions); this brief fixes the service side of each contract.
- `dispatch_dev_job`: separate future task `feat-dev-dispatch` (22).
- Chat deletion, transcript export, Ubuntu migration mechanics (layout is migratable: fixed `cwd`, one volume).
- Any change to ha-mcp policy or `wait_seconds`; applying `voice_policy.proposed.json` stays a user step.
- Parallel work inside one chat. Several tasks at once means several chats.

## 3. Architecture & module layout

```
xiaozhi provider ──POST /v1/chats/{id}/messages (SSE)──► agent-service ──stdio──► claude CLI (one per live chat)
gateway outbox   ◄──GET /v1/deliveries/stream──────────┤            └─ in-process MCP: ha (adapter), weixue (app tools)
device panel     ──POST /v1/chats/{id}/approve|deny|answer|interrupt
                                                       └──HTTP (HA_MCP_URL)──► ha-mcp (HA Green :9584, secret path)
                                                          office mode: from inside the wg namespace, like mcp-bridge
```

**Container**: `python:3.12-slim` pinned by digest (`vendor.lock`), pinned `requirements.txt` + `pip check`, non-root uid 1000; the wheel bundles the CLI (no Node). Paths: `/data/agent.db`, `/data/claude` (`CLAUDE_CONFIG_DIR`: transcripts, auto-memory), `/workspace` (fixed SDK `cwd`, read-only bind of `server/agent-service/workspace/`; part of every session and memory path, never changes). Nothing under `/` or `/workspace`'s parents may hold a `CLAUDE.md`.

**Networking** (17, no relay). agent-service calls `HA_MCP_URL` itself with mcp-bridge's switch `network_mode: ${WG_TUNNEL:+service:wg}`: project network at home, the `wg` namespace in office mode, started once the tunnel is ready, egress only via `wg0` (fix-wave5-server C7). Others use `AGENT_SERVICE_URL` (`http://agent-service:8010` or `http://wg:8010`, rendered by `setup.sh`). No LAN port; a `127.0.0.1:8010` publish (on `wg` in office mode) is allowed for readiness checks and the simulator. When a `wg` restart removes its `eth0` the service exits non-zero within 10 s; `restart: unless-stopped` rejoins the new namespace and recovery (section 9) runs. `setup.sh` generates `AGENT_SERVICE_TOKEN` (32 random bytes hex, `server/.env` 0600, never printed).

**Process model.** One `uvicorn --workers 1` process, Starlette/FastAPI + pydantic v2 (the schema source). One **ChatActor** per live chat owns its `ClaudeSDKClient` (streaming mode) and alone calls `query()`/`interrupt()`; others reach it through an `asyncio.Queue` (`message`, `event`, `interrupt`, `close`). It runs one continuous `receive_messages()` pump (not `receive_response()`: the 100-message buffer blocks control requests when undrained) and hands each message to the sink of the turn that owns it. One turn in flight per chat. All CLIs share the semaphore `CLI_PROCS_MAX` (default 4, re-set from S0 memory; `mem_limit` likewise); a busy actor is never evicted.

**Layout** (`server/agent-service/`): `weixue_agent/{__main__,config,jsonlog,ids,timeutil,schemas,store,api,sse,chats,turns,actor,sdk_options,routing,interactions,deliveries,recovery,weixue_tools,ha_adapter,monitor,health}.py`, `workspace/{CLAUDE.md,.claude/settings.json}`, `tools/{sim_device,e2e_conversation,export_schemas}.py`, `schemas/*.schema.json` (committed, drift fails a test), `tests/`. The bridge's `hamcp.py`/`config.py` move to a shared `server/weixue_hamcp/` installed in both images; `voice_filter.py` stays in the bridge; bridge tests stay green.

## 4. Conversation & job model

| Term | Meaning |
|---|---|
| Device | `device_id` = MAC (xiaozhi `Device-Id`); sim uses `sim-device-01` |
| Chat | one SDK session (`sdk_session_id` uuid4 chosen at creation) + index row; a device has `current_chat_id` and `previous_chat_id` |
| Turn | one `query()` in the chat session, from its prompt to its `ResultMessage`. `kind ∈ user \| steer \| event`; at most one in flight per chat |
| Task | the chat's in-flight work: opened by a user message when the chat has none, continued by steer and event turns in the **same session** (incl. waits between turns); at most one open per chat |
| Reply | the part of a turn that is spoken in the open voice exchange; it ends with `reply_end` (section 9) |
| Interaction | pending `input` (AskUserQuestion or a text question) or `approval` (HA held call, or SDK gate for a non-HA tool), `source ∈ sdk \| ha_mcp \| text` |
| Delivery | outbox row for the device: `agent` card, `chats` snapshot, spoken text (section 10) |

No forked task sessions, side turns, task actor or subagents: the model does long work in its own turn with full history, `AskUserQuestion` and auto-memory.

`task.state ∈ progress | needs_input | needs_approval | done | failed | cancelled` (last three terminal):

| State | Entered when |
|---|---|
| start → `progress` | a user message opens a task; its first turn is sent (event `started`) |
| `progress` | a turn is in flight; `Progress` from tool uses (`phase:"tool"`, name, `step` = count), text between tools (`label`) and `api_retry` |
| `needs_input` | `AskUserQuestion` reached `can_use_tool`, or a turn's final text asks something (`mcp__weixue__awaiting_answer`, or ends in `?`/`？`/`吗`; `source:"text"`, no options) |
| `needs_approval` | an HA call is held (section 8) or the SDK gate holds a non-HA tool |
| `done` | a turn ended `completed` with final text (`ResultMessage.result`) and nothing is pending |
| `failed` | SDK/API error, refusal, runaway guard, failed resume after a forced interrupt, `service_restart` |
| `cancelled` | `POST /chats/{id}/interrupt` (panel Stop, voice stop lexicon) |

`needs_*` return to `progress` when the task's next turn starts. Terminal tasks carry `outcome ∈ ok|partial|denied|expired|unknown|none` and `side_effects[]` (`confirmed|failed|unknown|not_run`) from `ToolUseBlock`/`ToolResultBlock(is_error)` pairs of non-read-only tools across its turns; a tool use with no result when its turn ends is `unknown`, and such a task is never reported as "cancelled cleanly" or "not done" (ADR-11). Turn state: `running → completed | interrupted | failed`; an interrupted turn does not end its task: the steer turn continues it, and the model decides whether to report progress, change course, continue or stop.

## 5. API (schemas)

Base `http://agent-service:8010/v1`; JSON UTF-8; RFC 3339 UTC ms; ids `tsk_|cht_|trn_|int_|dlv_` + 26-char ULID. Every route but `GET /healthz` needs `Authorization: Bearer <AGENT_SERVICE_TOKEN>`. `X-Trace-Id` (32 hex) generated when absent, echoed. UI text caps (title 32, summary 60, option 24, question 120) truncate with `…`, never reject. Errors `{"error":{"code","message","detail"}}`: `invalid_request` 400, `unauthorized` 401, `not_found` 404, `idempotency_mismatch|already_decided|expired|invalid_state|stale_lease` 409, `too_many_tasks` 429 (+`Retry-After`), `unavailable` 503 (CLI, process cap or key; never a value).

Idempotency: `POST /chats/{id}/messages` and `POST /chats` require `Idempotency-Key` (provider: `<device>:<xiaozhi session>:<turn seq>`; sim: uuid4; kept 24 h). Same key + hash → 200 `duplicate:true` (SSE replays from `Last-Event-ID`/`after`); different hash → 409. Decisions (`answer|approve|deny|interrupt`) carry a `decision_id`: first wins, repeat → 200 duplicate, conflict → 409 `already_decided`, expired → 409 `expired`.

| Route | Purpose |
|---|---|
| `POST /chats/{id}/messages` (`Accept: text/event-stream` or json) | user message: new task, steer, answer or voice decision (section 7); `{id}` may be `current` (created if none); SSE = chat events from `accepted` to this message's `reply_end`; header `X-Turn-Id` |
| `POST /chats/{id}/interrupt` | stop the turn in flight, cancel the open task |
| `GET /chats/{id}/events?after=` | all chat events; `Last-Event-ID` overrides `after`; `: hb` every `HEARTBEAT_S` |
| `POST /chats/{id}/approve\|deny\|answer` | decisions (`card_id = interaction_id`; the card carries `chat_id`) |
| `GET /chats?device_id&limit=12`, `POST /chats`, `GET /chats/{id}`, `POST /chats/{id}/switch`, `POST /chats/back`, `GET /chats/resolve?device_id&q`, `POST /chats/{id}/read` | chat index (feat-ui-sessions); `GET /chats/{id}` adds the open task and last 20 turns; switch prewarms, never interrupts |
| `GET /tasks/{id}`, `GET /tasks/{id}/trace` | task; trace (turns, events, redacted tool calls, interactions, deliveries, cost) |
| `GET /deliveries/stream?device_id`, `POST /deliveries/{id}/ack`, `GET /devices/{id}/snapshot`, `POST /devices/{id}/connection` | outbox (section 10); gateway connection events |
| `GET /healthz`, `GET /status` | health: DB writable, last CLI spawn ok, a matching successful ha-mcp `ping` result within 60 s (RPC error or result-less 202 is unhealthy); status: live clients, turns in flight, open tasks, pending deliveries, last API error, SDK/CLI versions |

Normative schemas (`$id` prefix `https://weixue.local/agent/v1/`, written by `tools/export_schemas.py`):

```json
{"$defs": {
  "TaskId": {"type":"string","pattern":"^tsk_[0-9A-HJKMNP-TV-Z]{26}$"},
  "ChatId": {"type":"string","pattern":"^cht_[0-9A-HJKMNP-TV-Z]{26}$"},
  "TurnId": {"type":"string","pattern":"^trn_[0-9A-HJKMNP-TV-Z]{26}$"},
  "InteractionId": {"type":"string","pattern":"^int_[0-9A-HJKMNP-TV-Z]{26}$"},
  "DeliveryId": {"type":"string","pattern":"^dlv_[0-9A-HJKMNP-TV-Z]{26}$"},
  "DeviceId": {"type":"string","minLength":1,"maxLength":64},
  "TraceId": {"type":"string","pattern":"^[0-9a-f]{32}$"},
  "Time": {"type":"string","format":"date-time"},
  "Lang": {"enum":["en","zh"]},
  "Tier": {"enum":["INTERRUPT","WHEN_IDLE","SILENT"]},
  "TaskState": {"enum":["progress","needs_input","needs_approval","done","failed","cancelled"]},
  "TurnState": {"enum":["running","completed","interrupted","failed"]},
  "Outcome": {"enum":["ok","partial","denied","expired","unknown","none"]},
  "Progress": {"type":"object","additionalProperties":false,"required":["phase"],
    "properties":{"step":{"type":["integer","null"]},"label":{"type":"string","maxLength":40},
      "tool":{"type":["string","null"],"maxLength":128},"phase":{"enum":["thinking","tool","writing","retrying","waiting"]}}},
  "SideEffect": {"type":"object","additionalProperties":false,"required":["tool","status"],
    "properties":{"tool":{"type":"string","maxLength":128},"target":{"type":["string","null"],"maxLength":128},
      "status":{"enum":["confirmed","failed","unknown","not_run"]}}},
  "Error": {"type":"object","additionalProperties":false,"required":["code","message"],
    "properties":{"code":{"enum":["service_restart","api_error","rate_limit","auth","billing","refusal","max_turns","max_budget","sdk_error","agent_process","interrupt_timeout","timeout","busy","internal"]},
      "message":{"type":"string","maxLength":300}}},
  "Option": {"type":"object","additionalProperties":false,"required":["id","label"],
    "properties":{"id":{"type":"string","maxLength":32},"label":{"type":"string","maxLength":24}}},
  "Interaction": {"type":"object","additionalProperties":false,
    "required":["interaction_id","task_id","chat_id","kind","source","state","created_at"],
    "properties":{"interaction_id":{"$ref":"#/$defs/InteractionId"},"task_id":{"$ref":"#/$defs/TaskId"},"chat_id":{"$ref":"#/$defs/ChatId"},
      "turn_id":{"anyOf":[{"$ref":"#/$defs/TurnId"},{"type":"null"}]},
      "kind":{"enum":["input","approval"]},"source":{"enum":["sdk","ha_mcp","text"]},
      "state":{"enum":["pending","answered","approved","denied","expired","cancelled","uncorrelated"]},
      "question":{"type":"object","additionalProperties":false,"required":["text"],
        "properties":{"text":{"type":"string","maxLength":120},"header":{"type":"string","maxLength":12},
          "options":{"type":"array","maxItems":4,"items":{"$ref":"#/$defs/Option"}},"multi_select":{"type":"boolean","default":false}}},
      "action":{"type":"object","additionalProperties":false,"required":["text","tool"],
        "properties":{"text":{"type":"string","maxLength":60},"detail":{"type":"string","maxLength":120},
          "tool":{"type":"string","maxLength":128},"risk":{"enum":["low","medium","high",null]},"reversible":{"type":"boolean"}}},
      "voice_can_approve":{"type":"boolean"},
      "created_at":{"$ref":"#/$defs/Time"},"expires_at":{"anyOf":[{"$ref":"#/$defs/Time"},{"type":"null"}]},
      "decision":{"type":["object","null"]},"via":{"enum":["panel","voice","api","timeout",null]}}}
}}
```

Exactly one of `question` (kind `input`) or `action` (kind `approval`) is present. `voice_can_approve` is true only for `reversible:true` actions (31).

`POST /chats/{id}/messages` request:

```json
{"type":"object","additionalProperties":false,"required":["device_id","text"],
 "properties":{"device_id":{"$ref":"defs.schema.json#/$defs/DeviceId"},
  "text":{"type":"string","minLength":1,"maxLength":4000},
  "lang":{"enum":["en","zh","auto"],"default":"auto"},
  "origin":{"type":"object","additionalProperties":false,"properties":{
    "kind":{"enum":["voice","panel","sim","api","retry"]},
    "gateway_session_id":{"type":"string","maxLength":128},"turn_seq":{"type":"integer","minimum":0}}},
  "retry_of":{"$ref":"defs.schema.json#/$defs/TaskId"}}}
```

Response (json, or the SSE stream's `accepted` data): `{chat_id, task_id, turn_id|null, routed: "new_task"|"steer"|"continue"|"answer"|"approval"|"cancel", duplicate}`. `lang: auto` is `zh` when the text contains CJK, else `en`.

Task: `{task_id, chat_id, device_id, state, title, summary, lang, progress, pending[], outcome, error, side_effects, turn_ids, current_turn_id, created_at, updated_at, finished_at, trace_id, retry_of, retry_allowed}` Turn: `{turn_id, task_id|null, chat_id, kind, state, terminal_reason, started_at, ended_at, reply_end_at, interrupted_by|null, cost_usd}`. Chat: `{chat_id, title, title_source, state: idle|working|needs_you|unread|failed, summary, unread, open_task_id, turn_in_flight, last_activity_at, created_at}`; `GET /chats` = `{device_id, current_chat_id, previous_chat_id, items}`, current first; `switch` = `{current_chat_id, previous_chat_id, last_summary}`; `resolve` = `{matches:[{chat_id,title,score}]}`, top 3.

Event envelope (`id: <seq>` / `event: <type>` / `data: <one line>`; `seq` is per chat):

```json
{"type":"object","additionalProperties":false,
 "required":["v","seq","chat_id","trace_id","ts","type","data"],
 "properties":{"v":{"const":1},"seq":{"type":"integer","minimum":1},
  "chat_id":{"$ref":"defs.schema.json#/$defs/ChatId"},
  "task_id":{"anyOf":[{"$ref":"defs.schema.json#/$defs/TaskId"},{"type":"null"}]},
  "turn_id":{"anyOf":[{"$ref":"defs.schema.json#/$defs/TurnId"},{"type":"null"}]},
  "trace_id":{"$ref":"defs.schema.json#/$defs/TraceId"},"ts":{"$ref":"defs.schema.json#/$defs/Time"},
  "type":{"enum":["accepted","started","text","reply_end","progress","interrupted","needs_input","needs_approval","resolved","done","failed","cancelled"]},
  "data":{"type":"object"}}}
```

| type | data |
|---|---|
| `accepted` | `{routed, task_state}`; first event of a message stream |
| `started` | `{kind, title}`; a turn was sent to the CLI |
| `text` | `{delta}` (`parent_tool_use_id is None`); on the message stream only until `reply_end`; persisted coalesced per 150 ms or sentence |
| `reply_end` | `{reason: "turn_done"\|"hold_elapsed"\|"ack"\|"question"\|"gateway_closed", ack_text: string\|null}`; once per message stream, then the stream ends |
| `progress` | `Progress`; at most one per 2 s per task except phase changes |
| `interrupted` | `{turn_id, by_turn_id\|null, terminal_reason, drain_ms, forced, unknown: [SideEffect]}` |
| `needs_input` / `needs_approval` | `{interaction, speak}` e.g. "I need your OK on the panel to turn on the desk plug." |
| `resolved` | `{interaction_id, state, via}` |
| `done` | `{summary ≤60, speak\|null (null when already spoken), tier, outcome, side_effects}` |
| `failed` | `{error: Error, speak, outcome, side_effects}` |
| `cancelled` | `{reason: "voice"\|"panel"\|"api", speak, outcome, side_effects}` |

Decision bodies:

```json
{"answer":{"type":"object","additionalProperties":false,"required":["interaction_id","decision_id","via"],
  "properties":{"interaction_id":{"$ref":"defs.schema.json#/$defs/InteractionId"},"decision_id":{"type":"string","format":"uuid"},
   "option_ids":{"type":"array","minItems":1,"maxItems":4,"items":{"type":"string","maxLength":32}},
   "text":{"type":"string","minLength":1,"maxLength":500},"via":{"enum":["panel","voice","api"]}},
  "oneOf":[{"required":["option_ids"]},{"required":["text"]}]},
 "approve|deny":{"type":"object","additionalProperties":false,"required":["interaction_id","decision_id","via"],
  "properties":{"interaction_id":{"$ref":"defs.schema.json#/$defs/InteractionId"},"decision_id":{"type":"string","format":"uuid"},
   "via":{"enum":["panel","voice","api"]}}},
 "interrupt":{"type":"object","additionalProperties":false,"required":["decision_id","reason"],
  "properties":{"decision_id":{"type":"string","format":"uuid"},"reason":{"enum":["voice","panel","api"]}}},
 "ack":{"type":"object","additionalProperties":false,"required":["lease_id","outcome"],
  "properties":{"lease_id":{"type":"string","format":"uuid"},
   "outcome":{"enum":["accepted","spoken","played","cut_off","shown","user_ack","deferred","dropped","lost"]},
   "proactive_id":{"type":"string","maxLength":128},"ttl_s":{"type":"integer","minimum":10,"maximum":3600},
   "retry_after_ms":{"type":"integer","minimum":0},"detail":{"type":"string","maxLength":200}}}}
```

AskUserQuestion answers go back as `PermissionResultAllow(updated_input={"questions": <original>, "answers": {<question text>: <label | [labels] | free text>}})` while its turn waits; after that turn ended (section 9) the answer is a new turn: `[answer to your earlier question "…"]: …`. Approve/deny on `source:sdk` resolves the `can_use_tool` future ("The user declined."); on `source:ha_mcp` it calls `ha_adapter.decide`. The `agent` payload uses the UI spec names (`concept-cards.md` §10), `progress→working`, `needs_input→needs_clarification` mapped only in `deliveries.py`.

## 6. SDK usage & sessions

`sdk_options.py` is the single `ClaudeAgentOptions` builder; every chat session uses the same options.

| Option | Value |
|---|---|
| `model` / `fallback_model` | `claude-fable-5-1` / `claude-opus-5-5` (15); a retention 400 switches to Opus, shown on the monitor (33) |
| `effort` | `low` until Q9 is answered (section 14); S1 (m) checks whether the pinned CLI accepts an effort change per turn |
| `thinking` | omit (always on for Fable 5.1; other values 400) |
| `max_turns` / `max_budget_usd` | 60 / 25.0 per turn, runaway guards only (15); S1 (i) checks whether `max_budget_usd` accumulates across `query()` calls of one process |
| `cwd` | `/workspace` |
| `permission_mode` | `"default"` explicitly |
| `setting_sources` | `["project"]` (loads `/workspace/CLAUDE.md`, `.claude/settings.json`) |
| `system_prompt` | omit (minimal default); all rules live in the root `CLAUDE.md` |
| `tools` | `["Read","Write","Edit","AskUserQuestion","WebSearch","WebFetch","TaskCreate","TaskUpdate","TaskList","TaskGet"]` + MCP (CLI ≥2.1.268 ships the Task* tools, not `TodoWrite`; UNVERIFIED that listing them is enough on Fable, else `CLAUDE_CODE_ENABLE_TODO_TOOLS=1`) |
| `allowed_tools` | `["mcp__ha__*","mcp__weixue__*","WebSearch","WebFetch"]` (19, 22) |
| `disallowed_tools` | `["Bash","Glob","Grep","NotebookEdit","Agent","Skill"]` |
| `can_use_tool` | `interactions.gate`: AskUserQuestion → `needs_input`; Read/Write/Edit allowed only under the memory dir, else deny; other non-HA tools → `needs_approval` (`source:sdk`) |
| `hooks` | none in v1 |
| `mcp_servers` | `{"ha": {"type":"sdk","name":"ha","instance": ha_adapter.server, "disableAutoBackground": True, "timeout": 30000}, "weixue": create_sdk_mcp_server(...)}`; `strict_mcp_config=True` |
| `include_partial_messages` | `True` (the reply streams per delta) |
| `env` | `{"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS":"1","CLAUDE_AGENT_SDK_CLIENT_APP":"weixue-agent/1","AGENT_SERVICE_TOKEN":"","HA_MCP_URL":""}`; `CLAUDE_CONFIG_DIR` at container level; retries/timeouts at SDK defaults (16) |
| `stderr` | redacting logger at DEBUG |

`mcp__weixue__*` app tools: `set_chat_title`, `notify_user(text, urgent)`, `set_result_tier(interrupt|silent)` (default WHEN_IDLE), `awaiting_answer()` (the final text is a question). No `start_background_task`.

Tool correlation comes from the stream, not hooks: `ToolUseBlock(id, name, input, parent_tool_use_id)` and `ToolResultBlock(tool_use_id, is_error)` fill `tool_calls` keyed by `turn_id`. Prompts are dict messages through `client.query(async_iter)` with `origin={"kind":"human"}`; a `ResultMessage` with `origin.kind=="human"` closes the turn in flight, anything else is logged as injected. `SystemMessage` subtypes `api_retry` (INFO + `progress{retrying}`), `model_fallback`, `model_refusal_no_fallback` are routed. `ResultMessage.model_usage` is cumulative per CLI process: keep a baseline reset on connect and on `ConversationResetMessage`, attribute each result to its turn.

**Lifecycle.** Connect with `resume=<sid>` when `$CLAUDE_CONFIG_DIR/projects/-workspace/<sid>.jsonl` exists, else `session_id=<sid>`. A failed `resume` never silently becomes a fresh session under the same chat id: the turn fails `agent_process`, the chat shows `failed`, the history association is kept and the user decides. Never `continue_conversation` (shared cwd) or `fork_session`. A switch prewarms with `connect()` and no prompt; the device UI is served from the index. Idle disconnect after `CLIENT_IDLE_S=300` with no turn in flight and nothing pending; LRU eviction takes idle actors only. Back-pressure: a message needing a new process waits `QUEUE_WAIT_MS`, then 503 `busy` and a spoken "I'm busy right now, try again in a moment" (zh 「我这会儿忙，稍后再试」). `CanUseToolShadowedWarning` at connect is expected and logged once.

**Index** (`store.py`, SQLite, `user_version` migrations): `devices`, `chats` (`sdk_session_id UNIQUE, title, title_source, summary, open_task_id, last_activity_at, last_read_seq`), `tasks` (`retry_allowed`, `result_tier`), `turns` (`kind, state, terminal_reason, started_at, reply_end_at, ended_at, interrupted_by, cost_usd`), `chat_notes` (context lines for the next turn), `tool_calls`, `chat_events` (coalesced text, pruned after `EVENT_RETENTION_DAYS`, 21), `task_transitions`, `interactions` (`external_ref` = ha-mcp token, never returned), `approvals`, `deliveries` (`lease_id, attempt, proactive_id, playback`), `idempotency`, `decisions`, `ha_tools_cache`, plus the monitor tables (section 12). Title: placeholder → first utterance → `set_chat_title`; no `rename_session`. S0 tests WAL on the real Rancher bind mount (`kill -9`, reopen, `integrity_check`, uid 1000), else a named volume or `journal_mode=DELETE, synchronous=FULL`.

## 7. Routing of new messages

`POST /chats/{id}/messages` for device D, chat C, in order:

1. **Spoken interaction window** (`VOICE_WINDOW_S=20`, across chats, from the last question or cue spoken inline or acked `played`). `input`: the text is the answer (`routed:"answer"`). `approval` (31): explicit phrases (en "approve / do it", zh 「批准 / 同意」, exact match) approve only a `voice_can_approve` card, the single one on screen; deny words (en "no / deny / cancel / stop", zh 「不 / 不要 / 拒绝 / 取消」) deny; a bare "yes / ok / 好 / 是" gets a repeat-back ("Say approve to turn on the desk plug, or tap the panel."); irreversible or money actions are tap-only. Other text falls through.
2. **Stop lexicon** (exact match: en "stop / cancel / never mind", zh 「停 / 停下 / 取消 / 算了」) while C has an open task → same as `POST /chats/C/interrupt{reason:"voice"}` (`routed:"cancel"`); the service speaks "Stopped." (zh 「好，停了。」) plus "Some of it may already have happened." when a side effect is `unknown`. Other phrasings go to the model as a steer.
3. **Turn in flight in C** → **steer** (`routed:"steer"`, section 9).
4. **Open task in C, no turn in flight** (waiting on an approval, a denied-in-turn question or a text question) → a `steer` turn of that task (`routed:"continue"`); a pending text question is answered by it.
5. Otherwise → a new task with a `user` turn (`routed:"new_task"`).

Panel answers apply to their interaction whatever the chat is doing. A chat switch never interrupts anything; deliveries keep their turn's `chat_id`. One chat never has two turns in flight; another chat runs in parallel in its own process.

## 8. HA adapter & approvals (ADR-9 inside this service)

**Exposure** (19): in-process `McpSdkServerConfig{"type":"sdk","name":"ha","instance": <mcp.server.Server>}`, one per chat actor (resolving the turn in flight at call time), sharing one `HaGateway` (streamable-HTTP client to `HA_MCP_URL` from `weixue_hamcp`, approvals table, pending poller). This adapter is the 8 s boundary. `list_tools` serves **every** ha-mcp tool from `ha_tools_cache`, unfiltered; a cache change calls `client.reconnect_mcp_server("ha")` (UNVERIFIED). Not `type:"http"` (no 8 s boundary; the secret URL would sit in the CLI's MCP config); no `PreToolUse` `defer` (a second gate).

**Call path** (`HA_CALL_BUDGET_S=8`, HTTP read timeout = `wait_seconds + 20` read from `/api/policy/config` at start; `approval_ttl_minutes` and `wait_seconds` are never hardcoded):

1. Dedupe key `(tool, sha256(canonical args))`. An identical call of ours in flight or pending adds this `(task_id, chat_id, turn_id, tool_use_id)` to that approval's **waiters** and returns its `PENDING_APPROVAL` text; no second request is sent.
2. Persist `approvals` `state=in_flight` (`approval_id, device_id, tool, args_json, args_hash, created_at, waiters_json`) before sending.
3. `create_task(ha.rpc(call))`, `wait(timeout=8.0)`; done in budget: return the result, `isError` passed through.
4. Else `state=pending`, return at once: `PENDING_APPROVAL id=<approval_id>: Home Assistant is holding <tool> <domain>.<service> <entity> for approval on the weixue panel. Do not call it again; the outcome will arrive as a follow-up message.` Emit `needs_approval` (card text from tool + args). The original request keeps waiting up to `wait_seconds`. Pending proves only that the result is late, not that HA holds the call.
5. **Correlation**: poll `GET /api/policy/pending` every 2 s while any approval is pending (once per minute otherwise). A row is **owned** only on exact identity: tool plus untruncated canonical args (or a request id if available), within ±10 s, exactly one candidate. Then `ha_token` (stored, never leaves the service) and `expires_at` fill the card; the card expires with the token (32). Otherwise `state=uncorrelated`: read-only card ("approve in the Home Assistant panel"), no device decision; another client's call is never adopted.
6. **Decisions** from the device go through `POST /api/policy/approve|deny {token}` (existing secret path) only for owned rows whose `device_id` matches; duplicates are idempotent per `approval_id`.

| Event | State |
|---|---|
| Original returns success | `executed` |
| Original returns `USER_DENIED` | `denied` |
| Original returns `USER_APPROVAL_REQUIRED` (wait elapsed) | `awaiting_decision_after_wait` |
| Then our own `/approve` call returns success (positive evidence; leaving `/pending` is no receipt) | persist `replay_dispatched`, then one `replay` of the identical call → `executed` or `failed`; a lost approve response is `unknown` |
| TTL passes, or `/approve` returns 404 | `expired`; the agent tells the user it lapsed and offers nothing automatic (32) |
| Transport error or service restart | `unknown`; never replayed (ADR-9: no blind retry) |

Terminal transitions close every waiter and add one event line per chat: `[event approval <id>] <tool> <summary>: executed | denied | expired | failed: <text> | result unknown. Already handled; do not call the tool again.` With no turn in flight the line starts an `event` turn of the waiting task; otherwise it goes to `chat_notes`, and an event turn follows the current turn unless another turn is queued. Event turns never interrupt, and are sent with `client_composed: True` (no `@path` expansion); their rules ("never retry an unknown result") live in the root `CLAUDE.md`. An HA request sent before its turn was interrupted keeps running here (approvals rows are not tied to turns); section 9 covers how it is reported.

**Cancel** denies owned tokens of the task through the same route; it is not optional. Uncorrelated holds keep a card reading "may still be waiting in the Home Assistant panel", never "cancelled". **Boot** reconciliation denies only pending tokens our `approvals` rows own; their tasks stay `unknown` until the deny result arrives; other pending calls are left and shown on the monitor. An HA restart drops ha-mcp's queue: a later decision gets `unknown_token`, mapped to `expired`.

**Gate model** (20, 31): HA tools are in `allowed_tools`, so ha-mcp policy alone decides held versus free and the panel (or an explicit phrase for reversible actions) approves held calls; `can_use_tool` covers only AskUserQuestion, the memory-dir path guard and any future non-HA tool (web tools are pre-allowed). A tool that can edit the policy or approve (S0/S1 item 4) goes to Jay, not into an app filter.

## 9. Turn lifecycle, interrupt & errors (ADR-11)

**Reply and acknowledgement.** No 90 s deadline, no foreground/background mode. The message stream (the provider's `chat()`) carries the reply and ends with `reply_end`; the device then runs its normal 8 s no-voice window. `reply_end` fires at the first of: `turn_done` (the turn ended; its final text was streamed, `done.speak` null); `hold_elapsed` (the first text segment ended in a tool use and `REPLY_HOLD_S=6` passed; text inside the hold is spoken); `ack` (no text within `ACK_AFTER_S=20` of the prompt: the service streams a canned "On it." / 「好的，我来处理。」, for a steer "One moment." / 「稍等。」); `question` (a question or approval cue was spoken); `gateway_closed` (the provider's SSE dropped: barge-in or disconnect; the turn keeps running, and `[note: your last reply was cut off and not heard]` goes to `chat_notes`). Root `CLAUDE.md` makes a short acknowledgement the model's first words for longer work; the canned ack is the safety net, so the device channel never idles past ~120 s. Text after `reply_end` is persisted, never spoken live.

**Result delivery.** A turn that ends with final text after its `reply_end` gives `done{speak}` in the task's `result_tier` (default WHEN_IDLE, section 10). Progress is spoken only when asked: "how is it going?" is a steer.

**Steer** (message while turn N is in flight):

1. Persist N+1 (`kind:"steer"`, unsent), emit `accepted{routed:"steer"}`; the device shows thinking; nothing of N is spoken from now on; N+1's `ACK_AFTER_S` clock starts.
2. Deny N's pending `can_use_tool` futures (`PermissionResultDeny("Interrupted by a new user message", interrupt=True)`, interaction `cancelled`); HA held calls are untouched.
3. `await client.interrupt()` bounded by `INTERRUPT_ACK_S=5` (the SDK's own control timeout is 60 s).
4. **Drain** until N's `ResultMessage` with `terminal_reason ∈ aborted_streaming|aborted_tools`, bounded by `DRAIN_S=10`. The actor stamps every message with the generation of the turn in flight and installs N+1's sink only after N's result, so buffered messages stay with N, are persisted and never spoken. N ends `interrupted`; its tool uses without a result are `unknown` (`interrupted.unknown`). An HA request already sent keeps running in the adapter; its later outcome reaches the chat as an event line; never retried.
5. Send N+1 in the same session: `[context: …]` + `chat_notes` + `[note: you were interrupted while working on "<title>"; the user's new message follows. Answer it; then continue, change course or stop as they ask.]` + an unknown-HA line naming calls still running ("result unknown; do not repeat them"), then the user text.

Latency from STT end to N+1's first token is drain + TTFT; S1 (l) measures it.

| Race | Rule |
|---|---|
| N's result is `completed` (finished first) | N+1 is a `continue` turn with `[note: your work on "<title>" finished just before this message; result: "<text>"; the user has not heard it]`; N's `done` card is SILENT, no spoken delivery |
| Interrupt ack or drain times out | `disconnect()`, N `failed{interrupt_timeout}` (open tool uses `unknown`), reconnect with `resume` and send N+1 with `[note: your previous turn was force-stopped]` |
| A second steer arrives before N+1 is sent | both texts go into one N+1, in order |
| Late interrupt hitting N+1 | impossible: N+1 is sent only after the ack and N's result |

**Interrupt route / cancel** (`POST /chats/{id}/interrupt`, stop lexicon, panel Stop): steps 2–4 without N+1; deny owned HA tokens of the task; task `cancelled{reason, outcome, side_effects}`; the next prompt in the chat gets `[note: the user stopped your previous work]`. A call already sent to HA "may have happened", never undone.

| Event | Rule |
|---|---|
| `TURN_MAX_S=1800` (runaway guard, not a voice deadline) | interrupt, task `failed{timeout}`, WHEN_IDLE notice |
| Question raised after `reply_end`, or unanswered `VOICE_WINDOW_S` after it was spoken | deny it with "The user has not answered yet; end the turn. Their answer will arrive as a new message"; task stays `needs_input`, card + WHEN_IDLE question |
| `AssistantMessage.error` / `ResultMessage.is_error`, `api_error_status` | `failed{rate_limit\|auth\|billing\|api_error\|max_turns\|max_budget\|sdk_error}`, spoken cue per code; every stream ends with `reply_end` or a terminal event |
| Fable `refusal` | `failed{refusal}`, "Sorry, I can't help with that one." (fallback coverage seen in S1) |
| `CLIConnectionError` / `ProcessError` | `failed{agent_process}`; next message resumes |
| Restart (`recovery.py`, before the API opens) | a turn in flight → its task `failed{service_restart}`, outcome `unknown` if a non-read-only call has no end, else `none`; WHEN_IDLE "I restarted while working on <title>, so it may not have finished" with `Retry` **only when `retry_allowed`**, else a "check state" follow-up; the chat's next turn gets a restart note naming unknown calls; tasks with no turn in flight keep their state; HA rows per section 8; SDK interactions `expired`; `CLAUDE_CODE_RESUME_INTERRUPTED_TURN` never set; deliveries per section 10; chats unchanged |

Nothing is retried after an unknown outcome. The SDK owns history; the service decides what the device hears and which context lines precede the next turn.

## 10. Delivery tiers & device queue

The service owns the durable outbox (tier, supersession, expiry, attempts); the gateway (feat-protocol-agent-msg) owns transport, device state and playback/user acks (35). One consumer per device: a new `GET /deliveries/stream` revokes the old; acks echo the offer's `lease_id` (stale → 409). Offers are ordered INTERRUPT, WHEN_IDLE, SILENT, then `gseq`.

**Spoken deliveries** ride the proactive queue (`docs/proactive.md`): the gateway posts `POST /weixue/proactive/messages` (`Idempotency-Key=<delivery_id>:<attempt>`, `device` always set, `urgent=(tier==INTERRUPT)`, `ttl=min(3600, remaining)`), acks `accepted{proactive_id, ttl_s}` (→ `handed_off`), polls `GET /weixue/proactive/messages/{id}`, acks `spoken`, then the device's `played` or `cut_off` (→ `delivered`; `cut_off` or proactive `partial` keeps the card unread "cut off"); `deferred` on `expired|failed` before any audio (→ `pending`, new key); 429 → `deferred` with `Retry-After`; 404/lost record → `lost`. **No replay without evidence** (35): a delivery whose speech may have started (`handed_off` past lease, `lost`, or a restart with no `played|cut_off`) is never spoken again; its card goes unread "may not have played". A user ack (tap, `read`, asking about it) clears unread. `done` speech stays valid 24 h.

| Source | Reply open | After `reply_end` | expires_at | Slot |
|---|---|---|---|---|
| `progress` | none | SILENT `agent{working}` | none | `task:<id>`, newest wins |
| `needs_input` | spoken inline; card SILENT | WHEN_IDLE question + card | `INPUT_TTL_S=1800` | `int:<id>` |
| `needs_approval` | spoken inline; card SILENT | WHEN_IDLE cue + card (INTERRUPT if urgent) | interaction `expires_at` | `int:<id>`, withdrawn on decision |
| `done` | inline; card SILENT `unread` | `result_tier` (default WHEN_IDLE) + card | 24 h speech, card stays | `task:<id>` |
| `failed` | inline | WHEN_IDLE one sentence + card | 24 h | `task:<id>` |
| `cancelled` | spoken by the service | SILENT | none | `task:<id>` |
| `notify_user(urgent=true)` | — | INTERRUPT | 1 h | own |
| chat index change | SILENT `chats` | SILENT `chats` | none | `chats`, coalesced |

INTERRUPT = proactive `urgent` (ignores mute; agent-declared urgency and HA safety only; urgent must bypass the quiescence wait); WHEN_IDLE waits for quiet, mute and TTL (delivered within 10 s of returning to idle); SILENT is cards only. A delivery superseded before hand-off is dropped; after hand-off the gateway marks it stale on arrival. A steer supersedes a pending WHEN_IDLE result of the same task (the model answers live). `card_shown`/`task_open` → `read` + ack `shown`. `GET /devices/{id}/snapshot` serves every pending card when a session opens.

## 11. Config, secrets & memory

`server/.env` (0600) is the single key source (16): `ANTHROPIC_API_KEY` (personal key), `HA_MCP_URL`, `AGENT_SERVICE_TOKEN`. Compose resolves `env_file` and `environment:` alike, so `docker compose config` never runs in gates or logs. agent-service gets a service-specific `environment:` with only those keys plus `TZ` and `CLAUDE_CONFIG_DIR`, never `env_file: .env`; the CLI keeps only the API key. `AGENT_SERVICE_TOKEN` reaches xiaozhi as an env var, never in `data/.config.yaml`. Constants (`config.py`): `REPLY_HOLD_S 6`, `ACK_AFTER_S 20`, `INTERRUPT_ACK_S 5`, `DRAIN_S 10`, `TURN_MAX_S 1800`, `HEARTBEAT_S 2` while a reply is open else 5, `QUEUE_WAIT_MS 1500`, `VOICE_WINDOW_S 20`, `INPUT_TTL_S 1800`, `CLI_PROCS_MAX 4`, `CLIENT_IDLE_S 300`, `MAX_OPEN_TASKS_PER_DEVICE 20` (429 `too_many_tasks`), `HA_CALL_BUDGET_S 8`, `HA_PENDING_POLL_S 2`, `EVENT_RETENTION_DAYS 30`. Error messages never echo values.

Compose: one `agent-service` definition in `server/docker-compose.yml` with `network_mode: ${WG_TUNNEL:+service:wg}`, the `environment:` above, volumes `./data/agent-service:/data`, `./agent-service/workspace:/workspace:ro`, `mem_limit` from S0, `restart: unless-stopped`, healthcheck `python -m weixue_agent.health` (15 s, start period 30 s). `mcp-bridge` is unchanged.

**Memory layout.** In git, read-only at `/workspace`: `CLAUDE.md` (identity; spoken style: turn language, one or two sentences, no Markdown; device state only from HA; acknowledge first for longer work, then do it in the same turn; how to read interrupt, `[event approval …]`, unknown-HA and restart notes; never retry an unknown result; asked for progress, say where you are in one sentence and carry on unless told otherwise; `AskUserQuestion` when an answer is needed), `.claude/settings.json` (allow `Read|Edit|Write(//data/claude/projects/-workspace/memory/**)`, deny rules mirroring `disallowed_tools`; exact rule syntax UNVERIFIED, S1). `can_use_tool` is not a filesystem guard: the boundary is the permission rules plus filesystem isolation (dedicated cwd, read-only workspace, no symlink escape), tested for traversal. Volume `/data/claude` (0700): transcripts, auto-memory `projects/-workspace/memory/`, `.claude.json`. Auto-memory stays on. Per-turn context (Dublin time, device, bedside/muted, reply language) is prefixed to the user text, not the system prompt, so the cache prefix stays stable.

## 12. Observability & monitor data

JSON lines to stdout (`jsonlog.py`): `ts, level, msg, trace_id, task_id, chat_id, turn_id, device_id, sdk_session_id, event, tool, outcome, latency_ms, model, cost_usd, attempt, code`; one line per task state change, turn start/end (`terminal_reason`), `reply_end` (reason, ms), interrupt (ack ms, drain ms, forced), tool call, interaction, delivery offer/ack, `api_retry`, `model_fallback`, SDK error, connect/resume/disconnect, recovery summary. Never text deltas; summaries ≤60 chars. Redaction: never the API key, HA URLs, tokens, `private_`/`token=` URLs, ha-mcp tokens; tool args logged as `args_redacted_json` (keys matching `/(token|pin|password|secret|key|url)/i` → `[redacted]`, strings capped at 120); SDK stderr through the same redactor. The trace id travels provider → service, so one grep across both containers' logs reconstructs a turn. Per turn TTFT, time to `reply_end`, time to result and cost; per task turns, interrupts and total time, in `/tasks/{id}/trace`. Gate logs saved redacted under `docs/logs/agent-service-<scenario>-<date>.log`.

**Monitor data** (feat-agent-monitor reads the SQLite file read-only): every tool call (name, args summary, result status, duration, `turn_id`, ha-mcp token id when held, approval decision + source + decided_at); every result's usage (tokens incl. cache, cost, model, turn kind) in `usage` + daily `usage_daily`; task and turn transitions incl. interrupts; `connection_events` (WS open/close per device); `health_probes` (ha-mcp reachable, tool count, pending count, policy version; mcp-endpoint; bridge; tunnel handshake age); `errors`. Retention per 21. The board binds 127.0.0.1, reached over Tailscale on the home server (30).

## 13. Test plan & e2e gate

**S0/S1 gate checklist** (first gate, from the Codex Wave 5 review; evidence per item in the spec; an office-mode deferral is outstanding, not passed):

1. The exact pinned SDK/CLI on arm64 with the personal key, the approved models, fallback and the retention behaviour (33); record the actual option and event shapes.
2. Session creation, restart/resume, **interrupt → drain → next turn in the same session** (S1 (l)), concurrent memory writes.
3. HTTP approvals against the installed ha-mcp version: early/late approve, deny, expiry, concurrent identical calls from another client, truncated-argument collisions, lost responses, restart of either service; `wait_seconds` untouched.
4. The complete HA tool surface against the effective policy, including indirect dispatch and policy/approval-management tools.
5. Fault tests of namespace replacement, deployment rollback, outbox partial delivery and crash after speech before ack.
6. The physical audio gate on the final stack (home): corrections during playback, all three cues followed by short answers, abort during cue loading, proactive failures, one real HA action, a spoken steer of a running task.

**Spike details.** S0: the pinned wheel in the arm64 image, CLI as uid 1000 with only `CLAUDE_CONFIG_DIR` and the key; WAL on the bind mount; idle/working CLI RSS (`CLI_PROCS_MAX`, `mem_limit`). S1 with `ClaudeSDKClient`: (a) `session_id` then `resume` after prewarm-only `connect()` and after restart; (c) AskUserQuestion reaches `can_use_tool`, answer shape accepted, deny-with-message ends the turn; (d) Task* tools on Fable via the `tools` list; (e) `api_retry`, `model_fallback`, `model_refusal_no_fallback` names; (f) cold `connect()` to first token with HA up and down; (g) memory: codeword recalled after restart in a new chat; 4 concurrent writers + restart keep `MEMORY.md` and `.claude.json` intact, else serialize via `mcp__weixue__remember` or ask Jay; (i) `max_budget_usd` accumulation across `query()` calls; (j) settings.json path-rule syntax; (k) whether ha-mcp executes an approved held call after the client's HTTP request dropped; (l) **`interrupt()` on the pinned SDK**, timed, during streaming text, during a running in-process MCP call, during a pending `can_use_tool`, and just after a turn completed: control-response latency; exactly one `ResultMessage` with `aborted_*` and its `origin`; all buffered messages before it and none of N after it; a late MCP result after the abort neither reaching N+1 nor corrupting the transcript; the next `query()` in the same `session_id` sees the interrupted turn (record the marker); an interrupt after completion is a no-op; `resume` of an interrupted session; (m) whether the CLI accepts an effort change between turns (Q9). Fallback if (l) fails: every steer takes the forced path (disconnect, `resume`, send N+1); API unchanged, latency recorded.

**Unit** (`tests/`, no network; `FakeSDKClient` scripting interrupt acks, buffered messages and `aborted_*` results with delays; fixtures recorded once live; rev 2's fake ha-mcp): `test_task_state`; `test_reply` (each `reply_end` reason, hold, canned acks, nothing streamed after `reply_end`); `test_steer` (generation attribution, `interrupted` with unknown effects, prefix lines, completed-before-interrupt race, timeout → forced resume, double steer merged, pending question denied, in-flight HA call → unknown then late event line, no retry); `test_cancel` (stop lexicon, interrupt route, owned tokens denied, uncorrelated wording); `test_routing` (spoken window, reversible-only phrases, "yes" repeat-back, tap-only, `continue`, parallel chat); `test_events` (event turn when idle, queued when busy); `test_interactions`; `test_idempotency`; `test_sse`; `test_recovery`; `test_chats`; `test_deliveries` (tiers, `played|cut_off|lost`, no replay without evidence, steer supersedes); `test_ha_adapter` (rev 2 list); `test_backpressure`; `test_schemas`; `test_redaction` (synthetic secrets absent from logs, trace, errors, MCP config, transcripts, monitor rows).

**Live SDK** (`RUN_LIVE_TESTS=1`, key sourced from `server/.env` in a subshell): short question with TTFT; codeword across resume and container recreate; S1 (l) against the real CLI.

**End-to-end gate (office mode)**: `tools/e2e_conversation.py` drives `tools/sim_device.py` as gateway and device; no audio; live HA over the tunnel; read-only entities plus `light.living_room_ceiling_lights` (34, restored after each scenario); unique run ids; every expectation asserted (non-zero exit on failure); redacted logs in `docs/logs/`.

1. **Short turn**: "Is the living room ceiling light on?" → one HA read, `reply_end{turn_done}`, `done.speak` null.
2. **Long task**: "Look at the living room lights and today's sunset time, check their history this week, and draft (don't apply) an automation that turns them on at sunset." → acknowledgement within `ACK_AFTER_S` (model or canned, logged), `reply_end` within `ACK_AFTER_S + REPLY_HOLD_S`, then ≥3 HA calls, total >30 s, `done` on `watch` as WHEN_IDLE (with `--busy`: deferred, lands within 10 s of un-busying).
3. **Interrupt mid-task**: rerun 2; after `reply_end` and one tool call send "How is it going?" → `interrupted` (drain ms logged), a streamed progress reply, more tool calls of the same task in the same `sdk_session_id`; then "stop" → `cancelled` within `INTERRUPT_ACK_S + DRAIN_S`, "Stopped." spoken, `side_effects` listed, no tool call after it.
4. **Denied approval**: held HA call → `needs_approval`; `POST /chats/{id}/deny` → our `/api/policy/deny` succeeds → `resolved{denied}` → event turn → `done{outcome:"denied"}`. Live only with Jay's policy overlay applied, else the fake ha-mcp (spec says which).
5. **Restart mid-turn**: `docker restart agent-service` during 2 → `failed{service_restart}`, WHEN_IDLE notice, `chats list` unchanged; the next message resumes the same `sdk_session_id` and the reply knows the work was cut off.
6. **Second chat**: during 2 in chat A, `chats new` and ask "What's the weather like?" → answered with `reply_end{turn_done}` while A still runs; no `interrupted` in A; two CLI processes in `status`.
7. **5-min task, device gone**: a ~5 min task (per-day history of three entities, step by step); the sim drops the stream at `reply_end` and disconnects; on reconnect the snapshot and a WHEN_IDLE delivery carry the result.
8. **Clarification**: "Remind me about the bins" → "Which day?" spoken, `reply_end{question}` → "Thursday" within `VOICE_WINDOW_S` → `done`.
9. **Free HA action** on the test entity: on, off, `ha_get_state` confirms, `side_effects: confirmed`, state restored.

Per CLAUDE.md the spec states that this task cannot run end to end on the device; feat-llm-claude-agent's gate covers wake → STT → agent → TTS incl. a spoken steer; feat-protocol-agent-msg / feat-ui-agent-states cover cards and acks.

## 14. Open questions for Jay

Answered: Q1 (31), Q2 (35, superseded by 36), Q3 (32), Q4/Q5 (orchestrator, section 8), Q6 (35), Q7 (33), Q8 (34). Open:

- **Q9 Effort in a shared session.** Decision 15 (low for chat, high for tasks) assumed separate task sessions; 36 puts tasks in the chat session. Options: (a) one effort for every turn (`low` fastest, `medium`, `high` slower short answers); (b) per-turn effort if S1 (m) shows support: `low`, raised to `high` for tool work. Recommendation: (b), else (a) `medium`. Build default `low` until answered.
- **Home audio window** (34): still to be named, for the deferred device-audio gate.

## 15. Follow-ups for the dependent tasks

- **feat-llm-claude-agent**: sync `httpx` SSE client to `POST /v1/chats/current/messages` (connect 2 s, read 15 s); `Idempotency-Key <device>:<session>:<turn>`, `X-Trace-Id`; yield `text`; return from `chat()` on `reply_end` or a terminal event, so xiaozhi runs its normal 8 s window; no 90 s deadline or task timer (36 supersedes tasks.yml's inherited line); on barge-in just close the stream; on stream drop try `GET /chats/{id}/events?after=` once, then the "can't reach my assistant" cue, no history write; `self.*` device tools are out of this contract; token via env; retire the sentence_id guard once voice HA calls move here.
- **feat-protocol-agent-msg**: consume `GET /v1/deliveries/stream` with the two-phase ack and `lease_id`; playback ack `played|cut_off` and user ack (35); `agent`/`chats` as `custom`; snapshot on session open; panel events `approval_decision/clarification_choice/card_later/card_shown/task_open/task_stop/chat_switch/chat_new` mapped to `approve|deny|answer`, `interrupt`, `read` and chat routes.
- **feat-ui-sessions**: `GET /chats` (≤12), `switch`/`back`/`resolve` (≥2 matches above 0.5 → clarification), `last_summary` toast, `read` on open; "new chat" starts a parallel task; single management view (27).
- **feat-approvals**: its approval source becomes this service's interactions plus ha-mcp policy; the held-call live test needs the policy overlay applied by Jay.
- **fix-wave3-server**: keep proactive status polling, `device` mandatory on agent posts, `urgent` bypassing the quiescence wait.

## 16. Existing worktree (`~/Projects/weixue-wt-feat-agent-service`, uncommitted, rev 2 model)

Build on it in place, squash at the end (~5.9 k lines in `server/agent-service/` plus edits to `server/{docker-compose.yml,.env.example,scripts/*}`).

**Reusable:** `Dockerfile`, `.dockerignore`, `requirements*.txt`/`.lock` (SDK 0.2.164), `pytest.ini`; `weixue_agent/{ids,timeutil,jsonlog,health,monitor,policy_model}.py` as is; `hamcp.py` moved to `server/weixue_hamcp/`; `ha_proxy.py` (section 8 adapter) with waiters bound to `(chat, task, turn, tool_use_id)` and outcomes routed as event lines/turns; `runtime.py` actor → `actor.py` (keep own-task connect/disconnect, continuous pump, `origin` attribution; add turn generations, a bounded `interrupt()` that awaits the ack and the drain instead of suppressing every exception, no eviction of busy actors); `store.py` (keep its tables incl. the monitor ones; `task_parts` → `turns`, add `chat_notes`, events per chat, drop `tasks.mode` and deadline columns); `deliveries, chats, events, recovery, schemas, api, service, config` restructured per sections 5, 9–11; from `tasks.py` tool correlation, gate, interactions, decisions, outcome/side-effect/error/usage helpers; `tests/{conftest,fake_sdk,fake_hamcp,test_ha_proxy}.py` (extend `fake_sdk` with interrupts), `test_recovery_outbox.py`/`test_api_misc.py` after re-expecting; `tools/export_schemas.py`; `workspace/.claude/settings.json`; `check_ready.py --agent-port`; the `setup.sh` token and `data/agent-service` steps.

**Drop or rewrite:** the forked-task model in `tasks.py` (`start_background_task`, `_start_background`, task actor and the `fork_session` branch in `sdk_options.py`, side turns, `_deadline`/`_background_now`/`_close_foreground`/`FOREGROUND_MAX_MS`, `release`, `_bg_timeout`, the main/task split of `TaskRun`, per-kind budgets); `/v1/tasks` write routes, `/v1/interactions/*`, `/v1/devices/{id}/spoken`; schemas `task-create`, `release`, `cancel`, `task`, `event`; old-model cases in `test_tasks.py`; the "Longer work" section and `[While you were busy …]`/`[background task …]` lines of `workspace/CLAUDE.md`; `agent-service/compose.yml` + `compose.tunnel.yml` via `include: compose${WG_TUNNEL:+.tunnel}.yml` and their `env_file: ../.env` (section 11 forbids it) → one service definition.

## Orchestrator notes on rev 3 (2026-10-07)

- Wire names: the device protocol (docs/briefs/feat-protocol-agent-msg.md) keeps `needs_input` on the wire; the gateway mapper is the only translator. Do not rename states in deliveries.
- Acks: add the `delivery_acks` table (delivery_id, kind played|cut_off|user_ack|lost, sentence_id, cut_at_ms, reason, device boot/seq, ts) written via POST /v1/deliveries/{id}/ack, joined to deliveries.playback; the monitor reads it.
- Q9 effort: resolved by the orchestrator within decision 15's intent — per-turn effort if S1 (m) shows the CLI supports it (low for short replies, high once a turn calls a tool or is steered), otherwise `medium` everywhere.
- Stop lexicon handled service-side with a canned "Stopped." is accepted (our stack).
- The old worktree attempt (§16) is reference only; nothing from it is committed blindly.
