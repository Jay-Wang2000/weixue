# feat-agent-service — developer brief (rev 4)

Rev 4 (2026-10-07) replaces the Claude Agent SDK with an in-process loop on the Anthropic Python SDK (40, ADR-10) and serves quick asks and tasks in one conversation with a per-turn model (39). Rev 3's runtime-independent contracts stay (sections 5, 7, 8, 10–12); rev 3 (git history) is the fallback design if the S0/S1 gate fails (section 13). SDK facts cite the installed `anthropic 1.11.0` source (`lib/tools/_beta_runner.py`, `mcp.py`, `_beta_builtin_memory_tool.py`) and the `claude-api` skill (cached 2026-09-25); UNVERIFIED items are checked in S1 first. Background: `~/Projects/research/reports/weixue-agent-runtime-assessment.md`, `docs/logs/latency-spike-2026-10-07.md`. Paths relative to `~/Projects/weixue`.

Fixed by Jay: decisions 15–41 in `docs/DECISIONS-PENDING.md`, which win over this brief (numbers in parentheses), plus the personal API key, ha-mcp `wait_seconds` unchanged (ADR-9) and ha-mcp policy as the only security gate (39). Open items: section 14. Never print secrets.

Jay's model: "I call Jarvis, 'do something for me'. It acknowledges; the exchange drops after 8 s of no voice. It keeps working and reports back when done. Same Claude session. While waiting I can wake it again and ask for progress or redirect, just like interrupting Claude Code."

## 1. Goal & scope

Build `server/agent-service/`: one asyncio process keeping one append-only Claude conversation per chat, with quick asks on Sonnet 5.5 and tasks on Fable 5.1 in that conversation; a new message steers the work in flight at the next tool boundary (a barge-in during a reply cancels the stream); it calls ha-mcp through an in-process adapter implementing the ADR-9 pending/approval contract, and reports results through a tiered, durable outbox. Closing the voice connection never cancels work.

In scope: container, actors, HTTP+SSE API with JSON Schemas, the turn loop, memory, web tools, HA adapter, errors (ADR-11), delivery tiers, config, observability, tests, `tools/sim_device.py` (gateway + device).

## 2. Non-goals

- The xiaozhi provider (feat-llm-claude-agent), device transport and playback/user acks (feat-protocol-agent-msg), device UI (feat-ui-agent-states, feat-ui-sessions); this brief fixes the service side of each contract.
- `dispatch_dev_job`: separate future task `feat-dev-dispatch` (22).
- The Claude Agent SDK and its harness features (CLAUDE.md, auto-memory, Skills, hooks, subagents).
- Chat deletion, transcript export, Ubuntu migration mechanics (one volume).
- Any change to ha-mcp policy or `wait_seconds`; applying `voice_policy.proposed.json` stays a user step.
- Parallel work inside one chat. Several tasks at once means several chats.

## 3. Architecture & module layout

```
xiaozhi provider ──POST /v1/chats/{id}/messages (SSE)──► agent-service (one process, ChatActor per chat)
gateway outbox   ◄──GET /v1/deliveries/stream──────────┤   └─ stream ─► Anthropic Messages API (+ server-side web, tool search)
device panel     ──POST /v1/chats/{id}/approve|deny|answer|interrupt   tools in-process: ha (adapter), app, memory
                                                       └──HTTP (HA_MCP_URL)──► ha-mcp (HA Green :9584, secret path)
                                                          office mode: from inside the wg namespace, like mcp-bridge
```

**Container**: `python:3.12-slim` pinned by digest (`vendor.lock`), pinned `requirements.txt`/`.lock` (`anthropic==1.11.0`, `mcp==2.3.0`, starlette, uvicorn, pydantic; no Node) + `pip check`, uid 1000. Paths: `/data/agent.db`, `/data/memory` (0700).

**Networking** (17, no relay). agent-service calls `HA_MCP_URL` itself with mcp-bridge's switch `network_mode: ${WG_TUNNEL:+service:wg}`: project network at home, the `wg` namespace in office mode, started once the tunnel is ready, egress only via `wg0` (fix-wave5-server C7). Others use `AGENT_SERVICE_URL` (`http://agent-service:8010` or `http://wg:8010`, rendered by `setup.sh`). No LAN port; a `127.0.0.1:8010` publish (on `wg` in office mode) is allowed for readiness checks and the simulator. When a `wg` restart removes its `eth0` the service exits non-zero within 10 s; `restart: unless-stopped` rejoins the new namespace and recovery (section 9) runs. `setup.sh` generates `AGENT_SERVICE_TOKEN` (32 random bytes hex, `server/.env` 0600, never printed).

**Process model.** One `uvicorn --workers 1` process, Starlette + pydantic v2 (the schema source), one `AsyncAnthropic` client (retries and timeouts at SDK defaults, 16) and one `HaGateway`. One **ChatActor** per live chat owns its transcript and running turn; others reach it through an `asyncio.Queue`. One turn in flight per chat; chats run in parallel up to `STREAMS_MAX` model streams, then 503 `busy` after `QUEUE_WAIT_MS` with a spoken "I'm busy right now, try again in a moment" (zh 「我这会儿忙，稍后再试」). An actor idle `ACTOR_IDLE_S` with nothing pending is dropped from memory and reloads from SQLite; a busy actor is never evicted.

**Layout**: `weixue_agent/{__main__,config,jsonlog,ids,timeutil,schemas,store,api,sse,chats,turns,actor,transcript,models,app_tools,memory,routing,interactions,deliveries,recovery,ha_adapter,monitor,health}.py`, `prompts/system.md`, `tools/{sim_device,e2e_conversation,export_schemas,spike_s1}.py`, `schemas/*.schema.json` (committed, drift fails a test), `tests/`. The bridge's `hamcp.py`/`config.py` move to a shared `server/weixue_hamcp/` installed in both images; `voice_filter.py` stays in the bridge; bridge tests stay green.

## 4. Conversation & job model

| Term | Meaning |
|---|---|
| Device | `device_id` = MAC (xiaozhi `Device-Id`); sim uses `sim-device-01` |
| Chat | one append-only conversation (`transcript` rows, section 6) + index row; a device has `current_chat_id` and `previous_chat_id` |
| Turn | one user-role message (`kind ∈ user \| steer \| event`) and the model requests that follow it until `end_turn`, a steer boundary or a stop; at most one in flight per chat |
| Mode | `quick` (Sonnet 5.5, effort low) or `task` (Fable 5.1, effort high), chosen per turn, switchable at a tool boundary (section 6) |
| Task | the chat's in-flight work: opened by a user message when the chat has none, continued by steer and event turns in the **same conversation** (incl. waits between turns); at most one open per chat |
| Reply | the part of a turn that is spoken in the open voice exchange; it ends with `reply_end` (section 9) |
| Interaction | pending `input` (the `ask_user` app tool, or a text question) or `approval` (HA held call), `source ∈ sdk \| ha_mcp \| text` (`sdk` name kept for schema stability) |
| Delivery | outbox row for the device: `agent` card, `chats` snapshot, spoken text (section 10) |

`task.state ∈ progress | needs_input | needs_approval | done | failed | cancelled` (last three terminal):

| State | Entered when |
|---|---|
| start → `progress` | a user message opens a task; its first turn is sent (event `started`) |
| `progress` | a turn is in flight; `Progress` from tool uses (`phase:"tool"`, name, `step` = count), progress notes (`thinking.display:"updates"`, as `label`) and SDK retries |
| `needs_input` | `ask_user` was called, or a turn's final text asks something (`awaiting_answer`, or ends in `?`/`？`/`吗`; `source:"text"`, no options) |
| `needs_approval` | an HA call is held (section 8) |
| `done` | a turn ended `end_turn` with final text and nothing is pending |
| `failed` | API error, refusal after fallbacks, runaway guard, `service_restart` |
| `cancelled` | `POST /chats/{id}/interrupt` (panel Stop, voice stop lexicon) |

`needs_*` return to `progress` when the task's next turn starts. Terminal tasks carry `outcome ∈ ok|partial|denied|expired|unknown|none` and `side_effects[]` (`confirmed|failed|unknown|not_run`) from `tool_use`/`tool_result(is_error)` pairs of non-read-only tools across its turns; a tool use with no result when its turn ends is `unknown`, and such a task is never reported as "cancelled cleanly" or "not done" (ADR-11). Turn state: `running → completed | interrupted | failed`; an interrupted turn does not end its task: the steer turn continues it, and the model decides whether to report progress, change course, continue or stop.

## 5. API (schemas)

Base `http://agent-service:8010/v1`; JSON UTF-8; RFC 3339 UTC ms; ids `tsk_|cht_|trn_|int_|dlv_` + 26-char ULID. Every route but `GET /healthz` needs `Authorization: Bearer <AGENT_SERVICE_TOKEN>`. `X-Trace-Id` (32 hex) generated when absent, echoed. UI text caps (title 32, summary 60, option 24, question 120) truncate with `…`, never reject. Errors `{"error":{"code","message","detail"}}`: `invalid_request` 400, `unauthorized` 401, `not_found` 404, `idempotency_mismatch|already_decided|expired|invalid_state|stale_lease` 409, `too_many_tasks` 429 (+`Retry-After`), `unavailable` 503 (model API, stream cap or key; never a value).

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
| `GET /healthz`, `GET /status` | health: DB writable, last model request ok, a matching successful ha-mcp `ping` result within 60 s (RPC error or result-less 202 is unhealthy); status: live clients, turns in flight, open tasks, pending deliveries, last API error, anthropic/mcp versions |

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
| `started` | `{kind, title}`; a turn's first request was sent |
| `text` | `{delta}` (`parent_tool_use_id is None`); on the message stream only until `reply_end`; persisted coalesced per 150 ms or sentence |
| `reply_end` | `{reason: "turn_done"\|"hold_elapsed"\|"ack"\|"question"\|"gateway_closed", ack_text: string\|null}`; once per message stream, then the stream ends |
| `progress` | `Progress`; at most one per 2 s per task except phase changes |
| `interrupted` | `{turn_id, by_turn_id\|null, terminal_reason, drain_ms, forced, unknown: [SideEffect]}` (`drain_ms` = wait to the boundary, `forced` = stream cancelled) |
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

`ask_user` answers resolve the held tool call while its turn waits; after that turn ended the answer is a new turn: `[answer to your earlier question "…"]: …`. Approve/deny on `source:ha_mcp` calls `ha_adapter.decide`; `source:sdk` approvals do not occur in v1 (no non-HA tool with side effects). The `agent` payload uses the UI spec names (`concept-cards.md` §10), `progress→working`, `needs_input→needs_clarification` mapped only in `deliveries.py`.

## 6. Model loop, transcript & tools

**Runner.** Each turn drives `client.beta.messages.tool_runner(..., stream=True, max_iterations=MAX_ITERATIONS)` (`BetaAsyncStreamingToolRunner`) built from the stored transcript. In the loop body the actor forwards stream events to the reply sink, reads `get_final_message()`, runs the calls with `generate_tool_call_response()` and appends message and results (plus any steer text) itself with `append_messages()`, so every message passes through `transcript.py` first. `run_tools_eagerly=False` in v1: tools run after the stream ends, so a cancelled stream ran nothing (S1 option: eager mode with `defer_tool_call()` for non-read-only HA calls). In 1.11 `pause_turn` and `compaction` resume (`_STOP_REASON_STEPS`; the skill's "runners don't resume `pause_turn`" note is for 0.116): the paused message is persisted and resent unchanged, nothing after it, at most `PAUSE_RESUME_MAX` per turn. `refusal`, `max_tokens`, `model_context_window_exceeded` stop the loop (section 9).

| Request field | Value |
|---|---|
| `model` | per turn: quick `claude-sonnet-5-5`, task `claude-fable-5-1` (39); after a retention 400 the chat's task model is `claude-opus-5-5`, shown on the monitor (15, 33) |
| `thinking` | `{"type":"adaptive","display":"updates","block_binding":{"prefix_mismatch_behavior":"drop_block"}}` (tests `"error"`); not `between_tools` (no per-message effort) |
| `output_config.effort` | top level fixed `low` (a change there restarts the cache); changes are appended effort-only messages `{"role":"system","content":[],"output_config":{"effort":"high"}}` |
| `fallbacks` | `"default"` on both models (open Q10) |
| `system`, `tools` | `prompts/system.md` (short, cached) and the tools below, stored per epoch, replayed byte-identical after restarts |
| betas | `server-side-fallback-2026-07-01`, `mid-conversation-output-config-2026-07-01`, `thinking-display-updates-2026-08-18`, `thinking-binding-controls-2026-08-01`, `compact-2026-09-04` |

**Tools** (fixed per chat epoch; a change breaks the cached and signed prefix):

- **HA** (19): every ha-mcp tool from `ha_tools_cache` as `async_mcp_tool(tool, gateway, defer_loading=…)`; `gateway` is `HaGateway` exposing `call_tool(name, arguments) -> CallToolResult` (a raw `ClientSession` would bypass the 8 s boundary).
- **Tool search**: `tool_search_tool_bm25_20251119` (or regex, S1 picks); HA tools `defer_loading:true` except `HOT_HA_TOOLS` (the bridge's 7 voice-path tools).
- **App** (`@beta_async_tool`): `ask_user(question, options[], multi_select)`, `start_task(title)`, `set_chat_title`, `notify_user(text, urgent)`, `set_result_tier(interrupt|silent)` (default WHEN_IDLE), `awaiting_answer()`.
- **Memory** `memory_20250818` (section 11); **web** (22, 41): server-side `web_search_20260209`, `web_fetch_20260209`, `max_uses` 5 each, no `user_location`.

A changed ha-mcp list applies to new chats and after a compaction. Per-model tool sets are allowed only as a pure function of the model (the skill's verified form): if a model rejects a tool version, and as the fallback if tool search misses the budget (S1 item 1): hot set + app tools on quick turns, full set on task turns.

**Mode switching** (39). A turn with no open task starts `quick`. Sonnet escalates by calling `start_task(title)` after a spoken acknowledgement: the result opens the task, the actor appends an effort-high message and the same loop continues on Fable; reaching `QUICK_MAX_ROUNDS` tool rounds escalates the same way. Turns of an open task run `task`; a steer inside a running loop keeps its model; a task closed at `end_turn` makes the next message `quick`. Thinking blocks are replayed unchanged on a switch, never stripped: Sonnet 5.5 drops Fable's blocks unbilled; Fable reads them again. Whether Fable reads Sonnet 5.5's blocks is UNVERIFIED; `model_binding_mismatch` entries are expected. Caches are per model (S1 item 6 times the first Sonnet turn after Fable work).

**Transcript** (`transcript.py`, sole writer): `transcript(chat_id, epoch, seq, role, content_json, model, turn_id, request_id, state)`, blocks verbatim (thinking with signatures, server-tool blocks, `compaction`, `fallback`).

1. Append-only; a request sends the current epoch. Rows stay `tail` until the response they were sent with completes; only such an uncommitted tail may be rewritten, once, by the cancel path (section 9): no thinking block was minted after it, so no signed prefix changes (S1 item 8).
2. Never a partial assistant message (no prefill; an unfinished thinking block has no signature). A message with a mid-stream `fallback` block is stored in the documented echo form (before the last `fallback` block only text and paired server-tool blocks).
3. Every `tool_use` gets a `tool_result` in the next user message: `is_error:true` "not run: <why>" or "result unknown: <why>; do not repeat it" when it never ran or its result is lost.
4. User words are a text block after the last `tool_result`, never inside one. Service notices (`[context …]`, `chat_notes`, interrupt, cut-off, restart, approval lines) are a separate `{"role":"system"}` message after it (last, or followed by an assistant turn). An event-only turn is a user message holding the `[event …]` line, which the system prompt attributes to the service.

**Compaction** (`compact-2026-09-04`): when a turn's last request input exceeds `COMPACT_AT_TOKENS`, the actor calls `runner.compact_before_next_turn({"type":"summarize","instructions": …})`; the runner compacts after the final message (skipped if tool calls are unanswered). The compaction message starts a new epoch and refreshes the tool list; the instructions keep the open task, pending interactions, unknown HA calls and the user's stated preferences.

**Usage.** Per request `usage`, model, effort, mode, `request_id`, TTFT, `input_transformations` → `usage` (section 12); cost from `config.py` prices. Runaway guards (15): `MAX_ITERATIONS` → `failed{max_turns}`; `TURN_BUDGET_USD` → stop at the next boundary, `failed{max_budget}`.

**Index** (`store.py`, SQLite, `user_version` migrations): the worktree's tables (section 16) with `sdk_session_id` replaced by `epoch` (plus `chats.mode`, `chats.task_model`, `turns.mode`), and `transcript`, `chat_prompts` (frozen `system` + `tools` per epoch). `interactions.external_ref` (the ha-mcp token) is never returned.

## 7. Routing of new messages

`POST /chats/{id}/messages` for device D, chat C, in order:

1. **Spoken interaction window** (`VOICE_WINDOW_S=20`, across chats, from the last question or cue spoken inline or acked `played`). `input`: the text is the answer (`routed:"answer"`). `approval` (31): explicit phrases (en "approve / do it", zh 「批准 / 同意」, exact match) approve only a `voice_can_approve` card, the single one on screen; deny words (en "no / deny / cancel / stop", zh 「不 / 不要 / 拒绝 / 取消」) deny; a bare "yes / ok / 好 / 是" gets a repeat-back ("Say approve to turn on the desk plug, or tap the panel."); irreversible or money actions are tap-only. Other text falls through.
2. **Stop lexicon** (exact match: en "stop / cancel / never mind", zh 「停 / 停下 / 取消 / 算了」) while C has an open task → same as `POST /chats/C/interrupt{reason:"voice"}` (`routed:"cancel"`); the service speaks "Stopped." (zh 「好，停了。」) plus "Some of it may already have happened." when a side effect is `unknown`. Other phrasings go to the model as a steer.
3. **Turn in flight in C** → **steer** (`routed:"steer"`, section 9).
4. **Open task in C, no turn in flight** (waiting on an approval, an unanswered `ask_user` or a text question) → a `steer` turn of that task (`routed:"continue"`); a pending text question is answered by it.
5. Otherwise → a new task with a `user` turn (`routed:"new_task"`).

Panel answers apply to their interaction whatever the chat is doing. A chat switch never interrupts anything; deliveries keep their turn's `chat_id`. One chat never has two turns in flight; another chat runs in parallel in its own actor.

## 8. HA adapter & approvals (ADR-9 inside this service)

**Exposure** (19): the runner's HA tools are `async_mcp_tool` wrappers over one `HaGateway` (streamable-HTTP client to `HA_MCP_URL` from `weixue_hamcp`, approvals table, pending poller); the actor binds each call to `(chat, task, turn, tool_use_id)` before it runs. This adapter is the 8 s boundary. Every ha-mcp tool is declared, unfiltered (section 6). Not the Messages API MCP connector (it needs a public URL, has no 8 s boundary, and the secret URL would leave the LAN).

**Session discipline** (as in `mcp-bridge`, `docs/ha-mcp.md`): `HaGateway` holds one MCP session per service process, opened lazily and shared by every chat, turn and tool call; never `initialize` per turn, per call or per reconnect. A `Mcp-Session-Id` is taken from the `initialize` response only, sent on every request, and ended with `DELETE` on shutdown and before a replacement session; a 404 for it reopens once on the next request. ha-mcp 8.6.0 runs stateless (no session id, `DELETE` → 405), so this is one `initialize` per process and no `DELETE`. `tools/list` (77 tools, ≈262 KB) has its own whole-exchange bound (20 s, headers through the last byte), separate from the tool-call read timeout; the runner declares tools from a cached copy at start and refreshes it in the background with 5–300 s doubling backoff, so a slow `tools/list` never blocks startup or a turn. Each refresh logs the session label, `sessions_opened`, `first_byte_ms` and `total_ms` with the keys of the bridge's `ha-mcp stats` line, for the monitor.

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

Terminal transitions close every waiter and add one event line per chat: `[event approval <id>] <tool> <summary>: executed | denied | expired | failed: <text> | result unknown. Already handled; do not call the tool again.` With no turn in flight the line starts an `event` turn of the waiting task; otherwise it goes to `chat_notes` and is appended as a system message at the running loop's next tool boundary (or with the next turn). Event turns never interrupt; their rules ("never retry an unknown result") live in the system prompt. An HA request sent before its turn was interrupted keeps running here (approvals rows are not tied to turns); section 9 covers how it is reported.

**Cancel** denies owned tokens of the task through the same route; it is not optional. Uncorrelated holds keep a card reading "may still be waiting in the Home Assistant panel", never "cancelled". **Boot** reconciliation denies only pending tokens our `approvals` rows own; their tasks stay `unknown` until the deny result arrives; other pending calls are left and shown on the monitor. An HA restart drops ha-mcp's queue: a later decision gets `unknown_token`, mapped to `expired`.

**Gate model** (20, 31, 39): ha-mcp policy alone decides held versus free and the panel (or an explicit phrase for reversible actions) approves held calls. The service gates nothing else: web tools are server-side and pre-allowed (22), memory is confined to `/data/memory`, `ask_user` is an interaction, not a gate. A tool that can edit the policy or approve (S0/S1 item 10) goes to Jay, not into an app filter.

## 9. Turn lifecycle, steer, cancel & errors (ADR-11)

**Reply and acknowledgement.** No 90 s deadline, no foreground/background mode. The message stream (the provider's `chat()`) carries the reply and ends with `reply_end`; the device then runs its normal 8 s no-voice window. `reply_end` fires at the first of: `turn_done` (the turn ended; its final text was streamed, `done.speak` null); `hold_elapsed` (the first text segment ended in a tool use and `REPLY_HOLD_S=6` passed; text inside the hold is spoken); `ack` (no text within `ACK_AFTER_S=20` of the prompt: the service streams a canned "On it." / 「好的，我来处理。」, for a steer "One moment." / 「稍等。」); `question` (a question or approval cue was spoken); `gateway_closed` (the provider's SSE dropped: barge-in or disconnect; see barge-in below). The system prompt makes a short acknowledgement the model's first words for longer work; the canned ack is the safety net, so the device channel never idles past ~120 s. Text after `reply_end` is persisted, never spoken live.

**Result delivery.** A turn that ends with final text after its `reply_end` gives `done{speak}` in the task's `result_tier` (default WHEN_IDLE, section 10). Progress is spoken only when asked: "how is it going?" is a steer.

**Steer** (message M while turn N is in flight; 36). No abort:

Persist N+1 (`kind:"steer"`, unsent), emit `accepted{routed:"steer"}`; nothing of N is spoken from now on; N+1's `ACK_AFTER_S` clock starts; a held `ask_user` resolves "No answer; the user's new message follows."; HA held calls are untouched. Then, at N's next boundary:

| N is | Action |
|---|---|
| in a tool round | after the results: user message `[tool_result…, text M]`, then system `[note: the user spoke while you were working on "<title>"; answer them, then continue, change course or stop as they ask]` + `chat_notes`; N ends `interrupted{terminal_reason:"steered"}`; N+1 continues the same loop and model |
| streaming | wait for the stream's end up to `STEER_WAIT_S`, then cancel it (barge-in path) |
| resuming `pause_turn` | wait; nothing goes after a paused message |
| finished (`end_turn`) | N+1 is a `continue` turn with `[note: your work on "<title>" finished just before this message; result: "<text>"; the user has not heard it]`; N's `done` card is SILENT |

A second steer before the boundary joins N+1, in order. HA calls already sent keep running in the adapter (outcome as an event line, never retried). STT end → N+1 first token = wait to the boundary + TTFT (S1 item 7).

**Barge-in** (37). When the provider's stream drops while reply text streams, or a steer's wait elapses, the actor cancels the task reading the stream (target < 500 ms to a closed HTTP stream). The partial assistant message is discarded: the conversation falls back to its last committed state; the spoken part survives only as context. The next request rewrites the uncommitted tail once into the same user content plus M (if any), then system `[note: your previous reply was cut off; the user heard: "<spoken text>"]`. After a completed message, a barge-in only adds that note to `chat_notes`.

**Interrupt route / cancel** (`POST /chats/{id}/interrupt`, stop lexicon, panel Stop): cancel the stream or tool coroutines (the adapter keeps HA requests already sent), close open tool uses ("not run: the user stopped this task" / "result unknown"), deny owned HA tokens of the task; task `cancelled{reason, outcome, side_effects}` within `CANCEL_MAX_S`; those results and `[note: the user stopped your previous work]` go with the next turn. A call already sent to HA "may have happened", never undone.

| Event | Rule |
|---|---|
| `TURN_MAX_S=1800` (runaway guard, not a voice deadline) | cancel, task `failed{timeout}`, WHEN_IDLE notice |
| Question raised after `reply_end`, or unanswered `VOICE_WINDOW_S` after it was spoken | `ask_user` resolves "The user has not answered yet; end the turn. Their answer will arrive as a new message"; task stays `needs_input`, card + WHEN_IDLE question |
| `RateLimitError`, `AuthenticationError`/`PermissionDeniedError`, billing 400, 5xx/529 after retries, `APIConnectionError`, stream `error` | `failed{rate_limit\|auth\|billing\|api_error}`, spoken cue per code; every stream ends with `reply_end` or a terminal event |
| Retention 400 on Fable | the chat's task model becomes `claude-opus-5-5`, one retry, monitor row (33) |
| `input_transformations` with `prefix_binding_mismatch` (or its 400 in tests) | `errors` row `code:"transcript"`; production continues (`drop_block`), tests fail |
| `refusal` after fallbacks | `failed{refusal}`, "Sorry, I can't help with that one."; nothing of the refused message is appended |
| `max_tokens` / context window exceeded | one continue / one compaction, else `failed{api_error}` |
| Restart (`recovery.py`, before the API opens) | a turn in flight → task `failed{service_restart}`, outcome `unknown` if a non-read-only call has no end, else `none`; WHEN_IDLE "I restarted while working on <title>, so it may not have finished" with `Retry` **only when `retry_allowed`**; open tool uses get "result unknown (service restarted); do not repeat it" plus a restart note with the next turn; other tasks keep their state; HA rows per section 8; `ask_user` `expired`; deliveries per section 10 |

`Error.code` keeps its enum: `sdk_error` = SDK or request error, `max_turns` = `MAX_ITERATIONS`; `agent_process` and `interrupt_timeout` are no longer emitted. Nothing is retried after an unknown outcome. The service owns the transcript and decides what the device hears and which notices precede the next request.

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

`server/.env` (0600) is the single key source (16): `ANTHROPIC_API_KEY` (personal key), `HA_MCP_URL`, `AGENT_SERVICE_TOKEN`. Compose resolves `env_file` and `environment:` alike, so `docker compose config` never runs in gates or logs. agent-service gets a service-specific `environment:` with only those keys plus `TZ`, never `env_file: .env`. `AGENT_SERVICE_TOKEN` reaches xiaozhi as an env var, never in `data/.config.yaml`. Constants (`config.py`): `REPLY_HOLD_S 6`, `ACK_AFTER_S 20`, `STEER_WAIT_S 3`, `CANCEL_MAX_S 2`, `TURN_MAX_S 1800`, `HEARTBEAT_S 2` while a reply is open else 5, `QUEUE_WAIT_MS 1500`, `STREAMS_MAX 8`, `ACTOR_IDLE_S 300`, `QUICK_MAX_ROUNDS 3`, `MAX_ITERATIONS 60`, `TURN_BUDGET_USD 25`, `PAUSE_RESUME_MAX 5`, `COMPACT_AT_TOKENS 150000`, `VOICE_WINDOW_S 20`, `INPUT_TTL_S 1800`, `MAX_OPEN_TASKS_PER_DEVICE 20` (429 `too_many_tasks`), `HA_CALL_BUDGET_S 8`, `HA_PENDING_POLL_S 2`, `EVENT_RETENTION_DAYS 30`, the model price table. Error messages never echo values.

Compose: one `agent-service` definition in `server/docker-compose.yml` with `network_mode: ${WG_TUNNEL:+service:wg}`, the `environment:` above, volume `./data/agent-service:/data`, `mem_limit` from S0, `restart: unless-stopped`, healthcheck `python -m weixue_agent.health` (15 s, start period 30 s). `mcp-bridge` is unchanged.

**Memory.** The `memory_20250818` tool backed by `BetaAsyncLocalFilesystemMemoryTool(base_path="/data/memory")`, subclassed to serialize writes behind one process-wide lock, cap file size and log each command (path only); the helper already rejects traversal and symlink escape (`_validate_path`, `_validate_no_symlink_escape`). No secrets in memory. `prompts/system.md` (in git, frozen per chat epoch, cached) carries rev 3's root `CLAUDE.md` rules: identity; spoken style (turn language, one or two sentences, no Markdown); device state only from HA; acknowledge before `start_task`, then do the work; how to read interrupt, cut-off, `[event approval …]`, unknown-HA and restart notes; never retry an unknown result; asked for progress, say where you are in one sentence and carry on unless told otherwise; `ask_user` when an answer is needed; check memory only when the ask needs it. Per-turn context (Dublin time, device, bedside/muted, reply language) goes in the system message after the user text, never in `system`. UNVERIFIED: whether the memory tool makes the model view its directory every turn; if so S1 picks an index in each epoch's first system message or the tool on task turns only.

## 12. Observability & monitor data

JSON lines to stdout (`jsonlog.py`): `ts, level, msg, trace_id, task_id, chat_id, turn_id, device_id, epoch, mode, event, tool, outcome, latency_ms, model, effort, cost_usd, attempt, code`; one line per task state change, turn start/end (`terminal_reason`), mode switch, `reply_end` (reason, ms), steer (boundary ms) and cancel (stop ms), tool call, interaction, delivery offer/ack, `api_retry` (`anthropic` logger retries), `model_fallback` (a `fallback` block), `input_transformations` entries, compaction, API error, actor load/evict, recovery summary. Never text deltas; summaries ≤60 chars. Redaction: never the API key, HA URLs, tokens, `private_`/`token=` URLs, ha-mcp tokens; tool args logged as `args_redacted_json` (keys matching `/(token|pin|password|secret|key|url)/i` → `[redacted]`, strings capped at 120); the `anthropic`/`httpx` loggers go through the same redactor. The trace id travels provider → service, so one grep across both containers' logs reconstructs a turn. Per turn TTFT, time to `reply_end`, time to result and cost; per task turns, steers and total time, in `/tasks/{id}/trace`. Gate logs saved redacted under `docs/logs/agent-service-<scenario>-<date>.log`.

**Monitor data** (feat-agent-monitor reads the SQLite file read-only): every tool call incl. server-side web calls (name, args summary, result status, duration, `turn_id`, ha-mcp token id when held, approval decision + source + decided_at); every request's usage (tokens incl. cache, server tool uses, cost, model, effort, mode, turn kind, TTFT) in `usage` + daily `usage_daily`; task and turn transitions incl. steers and cancels; `connection_events` (WS open/close per device); `health_probes` (ha-mcp reachable, tool count, pending count, policy version; mcp-endpoint; bridge; tunnel handshake age); `errors`. Retention per 21. The board binds 127.0.0.1, reached over Tailscale on the home server (30).

## 13. Test plan & e2e gate

**S0/S1 gate** (first gate; evidence per item in the spec, numbers in `docs/logs/feat-agent-service-s1-<date>.md`; an office-mode deferral is outstanding, not passed). Items 1–4 are the fallback trigger (40): if one fails within 3 working days, the task path reverts to rev 3's Agent SDK design; quick asks stay here.

1. **First token**: a quick turn with the real ha-mcp tool list (from the `wg` namespace; token count vs the stub's 91k) behind tool search: first-output median ≤ 800 ms on the spike's Q1/Q2 (1 warm-up + 5); else the hot-set quick tools ≤ 800 ms with `start_task` working.
2. **Cancel and steer on Fable**: stream cancel < 500 ms median; boundary steer and the next request 200 under `"error"`.
3. **Web**: `web_search` turns until a `pause_turn` occurs (or none in 20 tries, reported); the runner resumes it, the transcript holds the paused message once.
4. **One ADR-9 held HA call end to end**: pending at 8 s → card → panel approve via `/api/policy/approve` → `executed` → event line → `done` (needs Jay's policy overlay).
5. **Tool search, real 77 tools**: first output/text all-deferred vs hot set; searches per turn; wrong-tool rate on the e2e prompts; bm25 vs regex.
6. **Model switch in one chat**: quick → `start_task` → Fable → end → quick: no 400; `input_transformations`, cache read/creation and first token per request; both models accept the per-message effort and the same `tools`.
7. **Cancel/steer races**: steer during text, a tool round, a held `ask_user`, just after `end_turn`; double steer; cancel during a `pause_turn` resume; barge-in then a message 10 s later; each then one request in `"error"` mode.
8. **Append-only after interrupts**: the skill's `prefix_diff.py` over every request body of a 20-turn session with steer, cancel, barge-in, restart, switch and compaction shows only appends, the permitted tail rewrite and model switches.
9. **Basics**: pinned arm64 image as uid 1000; a Fable call with the personal key (33); WAL on the real bind mount (`kill -9`, reopen, `integrity_check`; else a named volume or `journal_mode=DELETE`); RSS for `mem_limit`; memory codeword in a new chat after restart, 4 concurrent writers; memory-tool view behaviour; whether ha-mcp executes an approved held call after the client's request dropped.
10. **Rev 3 items 3–6 unchanged** (sub-cases in rev 3 section 13): HTTP approvals against the installed ha-mcp; the HA tool surface against the effective policy; fault tests; the physical audio gate at home.

**Unit** (`tests/`, no network; a fake streaming Messages API scripting text, `tool_use`, `pause_turn`, `refusal`, `fallback` and `compaction` with delays; rev 2's fake ha-mcp): rev 3's suites (`test_task_state, test_reply, test_routing, test_events, test_interactions, test_idempotency, test_sse, test_recovery, test_chats, test_deliveries, test_ha_adapter, test_backpressure, test_schemas, test_redaction`, the last also over transcript and memory files) plus `test_steer` (boundary message shape, separate notes message, wait then cancel, races, held `ask_user`, in-flight HA call → late event line), `test_cancel` (partial discarded, tail rewritten once, open tool uses closed, owned tokens denied), `test_transcript` (each request = previous + appends except the tail rewrite; fallback echo form; compaction epoch; `system`/`tools` stable across restart), `test_modes`, `test_memory` (lock, traversal, symlink).

**Live** (`RUN_LIVE_TESTS=1`, key sourced from `server/.env` in a subshell): S1 items 1–9 via `tools/spike_s1.py`.

**End-to-end gate (office mode)**: `tools/e2e_conversation.py` drives `tools/sim_device.py` as gateway and device; no audio; live HA over the tunnel; read-only entities plus `light.living_room_ceiling_lights` (34, restored after each scenario); unique run ids; every expectation asserted (non-zero exit on failure); redacted logs in `docs/logs/`. The service-side first token per mode is recorded; speech-end → first audio ≤ 1.5 s (37) is measured in feat-llm-claude-agent's gate.

1. **Short turn**: "Is the living room ceiling light on?" → `quick`, one HA read, `reply_end{turn_done}`, `done.speak` null.
2. **Long task**: "Look at the living room lights and today's sunset time, check their history this week, and draft (don't apply) an automation that turns them on at sunset." → acknowledgement then `start_task` (switch to Fable logged), `reply_end` within `ACK_AFTER_S + REPLY_HOLD_S`, ≥3 HA calls, total >30 s, `done` on `watch` as WHEN_IDLE (with `--busy`: deferred, lands within 10 s of un-busying).
3. **Steer mid-task**: rerun 2; after `reply_end` and one tool call send "How is it going?" → `interrupted{steered}` (boundary ms logged), a streamed progress reply, more tool calls of the same task in the same transcript; then "stop" → `cancelled` within `CANCEL_MAX_S`, "Stopped." spoken, `side_effects` listed, no tool call after it.
4. **Denied approval**: held HA call → `needs_approval`; `POST /chats/{id}/deny` → our `/api/policy/deny` succeeds → `resolved{denied}` → event turn → `done{outcome:"denied"}`. Live only with Jay's policy overlay applied, else the fake ha-mcp (spec says which).
5. **Restart mid-turn**: `docker restart agent-service` during 2 → `failed{service_restart}`, WHEN_IDLE notice, `chats list` unchanged; the next message continues the same transcript and the reply knows the work was cut off.
6. **Second chat**: during 2 in chat A, `chats new` and ask "What's the weather like?" → answered (web search) with `reply_end{turn_done}` while A still runs; no `interrupted` in A; two busy actors in `status`.
7. **5-min task, device gone**: a ~5 min task (per-day history of three entities, step by step); the sim drops the stream at `reply_end` and disconnects; on reconnect the snapshot and a WHEN_IDLE delivery carry the result.
8. **Clarification**: "Remind me about the bins" → `ask_user` "Which day?" spoken, `reply_end{question}` → "Thursday" within `VOICE_WINDOW_S` → `done`.
9. **Free HA action** on the test entity: on, off, `ha_get_state` confirms, `side_effects: confirmed`, state restored.
10. **Barge-in**: the sim drops the stream mid-answer and sends a correction → stream cancelled < 500 ms, no partial assistant message stored, the reply follows the correction.

Per CLAUDE.md the spec states that this task cannot run end to end on the device; feat-llm-claude-agent's gate covers wake → STT → agent → TTS incl. a spoken steer and barge-in; feat-protocol-agent-msg / feat-ui-agent-states cover cards and acks.

## 14. Open questions for Jay

Answered: Q1 (31), Q2 (35, superseded by 36), Q3 (32), Q4/Q5 (orchestrator, section 8), Q6 (35), Q7 (33), Q8 (34), Q9 (per-turn model and effort, 39). Open:

- **Q10 Refusal fallback target.** Decision 15 names `claude-opus-5-5`, but per the skill Fable 5.1's server-side fallbacks only target Opus 4.8 and Opus 5. Options: (a) `fallbacks:"default"`, server-routed, no extra request (recommended; matches 39); (b) a client-side retry on `claude-opus-5-5` (one more request). Build default (a); the retention-400 switch to Opus 5.5 (33) stays.
- **Home audio window** (34): still to be named, for the deferred device-audio gate.

## 15. Follow-ups for the dependent tasks

- **feat-llm-claude-agent**: every ask goes to the service (39, 40); no direct Sonnet path or `hand_off_to_agent` unless the fallback trigger fires. Sync `httpx` SSE client to `POST /v1/chats/current/messages` (connect 2 s, read 15 s); `Idempotency-Key <device>:<session>:<turn>`, `X-Trace-Id`; yield `text`; return from `chat()` on `reply_end` or a terminal event, so xiaozhi runs its normal 8 s window; no 90 s deadline or task timer (36); on barge-in close the stream (the service cancels the generation) and send the new utterance; on stream drop try `GET /chats/{id}/events?after=` once, then the "can't reach my assistant" cue, no history write; `self.*` device tools are out of this contract; token via env; retire the sentence_id guard once voice HA calls move here.
- **feat-protocol-agent-msg**: consume `GET /v1/deliveries/stream` with the two-phase ack and `lease_id`; playback ack `played|cut_off` and user ack (35); `agent`/`chats` as `custom`; snapshot on session open; panel events `approval_decision/clarification_choice/card_later/card_shown/task_open/task_stop/chat_switch/chat_new` mapped to `approve|deny|answer`, `interrupt`, `read` and chat routes. The device protocol keeps `needs_input` on the wire; the gateway mapper is the only translator.
- **feat-ui-sessions**: `GET /chats` (≤12), `switch`/`back`/`resolve` (≥2 matches above 0.5 → clarification), `last_summary` toast, `read` on open; "new chat" starts a parallel task; single management view (27).
- **feat-approvals**: its approval source becomes this service's interactions plus ha-mcp policy; the held-call live test needs the policy overlay applied by Jay.
- **fix-wave3-server**: keep proactive status polling, `device` mandatory on agent posts, `urgent` bypassing the quiescence wait.

## 16. Salvage from the stopped worktree

`~/Projects/weixue-wt-feat-agent-service`, branch `feat/agent-service`, wip commit `8fa8dc0` (76 files, +9.8 k lines; its 82 unit tests pass in its own venv on 2026-10-07). Build on it in place and squash; nothing is committed blindly.

**Reusable** (light edits):
- Build: `Dockerfile` (drop `CLAUDE_CONFIG_DIR`), `.dockerignore`, `pytest.ini`, `requirements*.txt`/`.lock` (replace `claude-agent-sdk` with `anthropic==1.11.0`, re-lock).
- Store: `store.py`, all 20 tables incl. `delivery_acks` and the monitor tables; `sdk_session_id` columns → `epoch`/`mode`; add `transcript`, `chat_prompts`.
- `api, sse, schemas, chats, routing, deliveries, recovery` (add the open-tool-use rule), `config` (section 11), `ids, timeutil, jsonlog, health, monitor, policy_model`; `schemas/*.schema.json`, `tools/export_schemas.py`.
- HA adapter: `ha_adapter.py` `HaGateway` (8 s budget, dedupe/waiters, pending poller, exact-match ownership, `decide`, boot reconciliation): keep `call()`, replace `mcp_server()` (SDK in-process server) with the `call_tool` surface; `server/weixue_hamcp/` and the bridge edits.
- Interactions: `interactions.py` state, decision and expiry logic; its `PermissionResult*` gate becomes the `ask_user` futures.
- Tests: `conftest`, `fake_hamcp` and every suite not listed under Rewrite; `tools/sim_device.py`, `tools/spike_hamcp.py`.

**Rewrite**:
- `actor.py` (SDK client pump, generations, interrupt and drain) → runner-driven actor + `transcript.py`.
- `turns.py`: keep intake, `_enqueue`, reply/ack/`end_reply`/`gateway_closed`, the event-turn queue, progress throttling, side-effect/outcome helpers, `finish_task`; rewrite `_run_turn`, `_on_message`, `_tool_use`/`_tool_result`, `_stop_current`, `result_error`, `_account`, `_finalize`.
- `sdk_options.py` → `models.py` (section 6); `weixue_tools.py` (`create_sdk_mcp_server`) → `app_tools.py`; `service.py` SDK/CLI version probes → anthropic/mcp versions.
- `tests/fake_sdk.py` → the fake stream; `test_steer`, `test_cancel`, `test_reply`, `test_recovery`, `test_task_state` re-expected for boundary steer and stream cancel.
- `tools/spike_s1.py` (SDK spike) → new S1 script; `workspace/CLAUDE.md` → `prompts/system.md`; `workspace/.claude/settings.json` dropped.

## Follow-ups filed from the gateway brief (2026-10-10)

- `device_action` tool: under the gateway's `nointent` mode the device MCP tools (bedside, volume, brightness) are unreachable from the model; the service exposes a `device_action {tool, args}` app tool emitted as an SSE event that the gateway executes on the device and acks back. Small follow-up after the first merge.
- TextCoalescer holds the first delta up to 150 ms: make the first delta flush immediately (latency red line).
- Barge-in contract with the gateway: SSE close = gateway_closed (drop partial, keep "the user heard:" note, resume the task after 10 s); `/interrupt` stays the explicit cancel.
