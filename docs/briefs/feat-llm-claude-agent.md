# feat-llm-claude-agent — developer brief (rev 1, 2026-10-10)

Sources: tasks.yml, DESIGN.md (ADR-7/9/11, M2 row), decisions 31, 35–45, `docs/briefs/feat-agent-service.md` rev 4 §5/§9/§10/§15, the service code on `feat/agent-service` (wins where it differs), `docs/agent-protocol.md`, the latency spike log. Never print secrets.

## 1. Scope

A xiaozhi LLM provider `type: claude_agent` (`server/overlay/core/providers/llm/claude_agent/claude_agent.py`) that sends every voice turn to agent-service and streams its reply text into xiaozhi's TTS path. The service decides quick vs task, model, tools, steer and delivery; the gateway carries text and owns transport, timing and TTS. Also in scope: the first-clause TTS rule, per-turn latency metrics, the config switch with rollback.

Not in scope: device cards/acks (feat-protocol-agent-msg), voice chat commands (feat-ui-sessions), service changes (section 10).

## 2. Turn mapping

- **Entry.** With `claude_agent` selected, `Intent` is `nointent`, so upstream `ConnectionHandler.chat()` calls `llm.response(session_id, dialogue)` and treats every yield as text. The provider sends only the last user message; the service owns the transcript, and xiaozhi's local `dialogue` is ignored.
- **Connection lookup.** `register_sessions` also keeps `session_id → conn` (device id, STT language, turn counter, active-stream slot).
- **Request.** Sync `httpx` (0.28.1, in the upstream image) `POST {AGENT_SERVICE_URL}/v1/chats/current/messages`, `Accept: text/event-stream`, bearer `AGENT_SERVICE_TOKEN` (env only, never in `.config.yaml`), `Idempotency-Key: <device>:<session_id>:<turn seq>`, `X-Trace-Id` (new 32-hex per turn, logged). Body: `{device_id, text, lang: en|zh|auto, origin:{kind:"voice", gateway_session_id, turn_seq}}`. Timeouts: connect 2 s, read 6 s (3 missed `: hb` frames; the service sends one every `HEARTBEAT_REPLY_S=2` while a reply is open).
- **Chat selection.** Always `current`. The device's `switch_chat`/`new_chat` already reach `/chats/{id}/switch` and `POST /chats` through `ServiceSink`; the service resolves `current` from its device row and returns `X-Chat-Id`. Race: a tap followed at once by speech. `ServiceSink` exposes a per-device future for an in-flight switch/new-chat post, and the provider waits for it (≤ 1 s) before posting.
- **Stream.** Yield each `text.delta` as soon as it arrives. Yield `""` on every heartbeat so `chat()` re-checks `client_abort` at least every 2 s. Return at `reply_end`, whatever its `reason` (`turn_done | hold_elapsed | ack | question | gateway_closed`). The service ends the stream only at its own `reply_end` and speaks failures inline as `text`, so terminal events need no handling. `ack_text` is informational: the ack has already arrived as a `text` delta, so it is never spoken twice. Then `chat()` writes LAST and xiaozhi's normal 8 s no-voice window runs. No deadline or task timer (36).
- **Inherited from fix-turn-isolation.** The 90 s deadline is gone (36). Stale-turn guard: each `chat()` gets a generation, and text from a superseded generation is never queued (upstream TTS also drops a mismatched `sentence_id`). Network errors are handled in section 6; model retries and their logs live in the service.

## 3. First-clause TTS (`google_tts.py`)

Today the first segment goes out at the last clause mark in the buffer (base `first_sentence_punctuations`), at an English sentence end followed by whitespace, after 250 characters, or at LAST. A one-sentence reply therefore waits for its end (spike: +0.9 s), and so does "On it." with no trailing space.

New rule, only while `is_first_sentence` (config `first_clause: true`, default on):

1. Cut at the **earliest** clause or sentence mark (`， , 、 ： : ； ; 。 . ! ? ！ ？ … —`) once the prefix holds ≥ 2 words (en) or ≥ 4 CJK characters. A `.` at the end of the buffer counts (abbreviation and initial rules unchanged).
2. With no mark, cut at the last word boundary once the prefix holds 8 words (en) or 12 CJK characters.
3. Later segments keep the current sentence rule. LAST flushes as today.

Each segment is its own `streaming_synthesize` request (first audio ≈ 0.2 s); the rule also serves the legacy provider.

## 4. Acknowledgement while tools run

There is one owner per layer, and nothing is duplicated:

- **Model** (service system prompt, already in `prompts/system.md`): a short first sentence that answers or acknowledges, spoken before any tool call ("Let me check."). This is the primary acknowledgement and the only way to meet ≤ 2.5 s with an HA read.
- **Service**: a canned "On it." / 「好的，我来处理。」 (steer: "One moment." / 「稍等。」) streamed as `text` when nothing was spoken within `ACK_AFTER_S=20`, then `reply_end{ack}`. This is the safety net under the device's ~120 s channel.
- **Gateway**: speaks nothing of its own while the service stream is alive (heartbeats count). It adds a line only when the connection itself fails (section 6).

## 5. Barge-in, steer, stop

- **Barge-in** (37). An abort (wake, tap, IMU, or a transcript while speaking) makes the provider close the open SSE response at once, from the abort hook rather than at the next yield, because upstream resets `client_abort` when the next `chat()` starts. Queued, unyielded text is dropped; upstream `clear_queues` clears TTS. Hook: `count_aborts` (an event generation) calls `conn.weixue_agent_stream.cancel()`. The service sees the drop as `gateway_closed`, discards the partial assistant message, keeps the heard part as a cut-off note, and resumes task work after `BARGE_IN_RESUME_S=10` if nothing follows. The new utterance is the next POST.
- **Deviation from the task text: the provider does not call `POST /chats/{id}/interrupt` on barge-in.** In the service, interrupt cancels the open task ("Stopped."), which belongs to the panel Stop and the voice stop lexicon (the service handles the lexicon itself). Closing the stream is the barge-in.
- **Steer**: a new utterance while a task runs, after `reply_end`, is a plain POST. The service routes it (`steer`/`continue`/`answer`/`approval`) and owns spoken answers and approvals within `VOICE_WINDOW_S`.

## 6. Errors and stalls

| Case | Action |
|---|---|
| Connect error or 5xx before `accepted` | one retry after 300 ms with the same Idempotency-Key, then the fallback line |
| 503 `busy` | speak the service's `detail.speak` if present, else the fallback; no retry |
| 401/400/409 | fallback line + error log (`code` only) |
| Stream drop or 6 s read stall after `accepted` | once: `GET /v1/chats/{chat_id}/events?after=<last seq>`, filtered to this turn until `reply_end`; else the fallback line |
| 3 failures in 60 s | breaker: for 30 s skip the POST and speak the fallback at once; a log line on open and close |

Fallback line: "I can't reach my assistant right now." / 「我暂时连不上助手。」 It is spoken through the same `sentence_id` and is never written to history. If a 6 s stall lands after text was spoken, the line is "Sorry, I lost the connection."

## 7. Wake cue, echo guard

- **Wake cue** (the fix-wave5 C3 event-generation fence): unchanged. A transcript calls `note_event` + `cancel_wake_cue` in `guard_playback_echo` before `startToChat`, so a cue never plays over the provider's turn. Streaming STT finals (0.2–0.3 s) arrive before `CUE_DELAY_S=0.6` on one-breath wakes. A test asserts that no cue frame follows a turn's FIRST.
- **Echo guard**: unchanged; an echo never reaches the service.

## 8. Config switch, retirement, dead code

`server/.env` `VOICE_LLM=claude_agent|anthropic` (default `claude_agent` after the gate), rendered by `render_config.py`. `claude_agent` sets `selected_module.LLM: ClaudeAgentLLM` and `Intent: nointent`, and wires `AGENT_SERVICE_URL`/`AGENT_SERVICE_TOKEN` into xiaozhi's `environment:`. `anthropic` restores today's `AnthropicLLM` + `function_call` + the mcp-bridge voice tools. The live switch is `setup.sh` (rebased on `main` per office-mode rule).

While `claude_agent` is selected, these become dead:

- `weixue_patches.decode_direct_answer`, `keep_decoded_text`, `_hex4`, `_RESPONSE_VALUE`, `_JSON_ESCAPES` (`direct_answer` exists only under `function_call`);
- `overlay/core/providers/llm/anthropic/anthropic.py`;
- the bridge's voice tool path (`voice_filter.py`, `policy/voice_bridge.json`, decision 5a). The bridge keeps running for health and the monitor;
- the DESIGN row's "MCP endpoint executor passes timeouts through" (no voice MCP calls remain).

Everything else in `weixue_patches` stays live (wake cue/hold, bare-wake filter, echo guard, playback record feeding `played` acks, idle-timer hold, proactive invoke, allow-lists).

**Recommendation: keep all of it behind the switch until the M2 milestone E2E passes**, then a cleanup task deletes it with its tests. Both paths stay selectable for rollback.

## 9. Latency and metrics

Targets (37, spike), measured from the server VAD end-of-utterance to the first Opus frame written to the device:

- quick ask ≤ 1.5 s median = STT final 0.3 + service first text ≤ 0.8 + TTS first clause 0.2–0.3 + transport/queue ≤ 0.1;
- one HA read ≤ 2.5 s to first audio, which requires the model's pre-tool sentence; the answer's first audio is logged as `answer_audio_ms`.

One log line per turn: `turn_latency trace=… chat=… routed=… reply_end=<reason> stt_final_latency_ms=… service_first_text_ms=… tts_first_audio_ms=… total_ms=… answer_audio_ms=… reply_end_ms=…`.

| Key | Measured from → to |
|---|---|
| `stt_final_latency_ms` | already logged by `google_stt_stream.py` |
| `service_first_text_ms` | POST sent → first non-empty `text` |
| `tts_first_audio_ms` | first segment handed to TTS → its first frame |
| `total_ms` | speech end → first frame |

## 10. Follow-ups for agent-service (not done here; orchestrator files them)

1. `TextCoalescer` holds the first delta of a reply up to 150 ms. It should flush the first delta at once.
2. `heard` is streamed text, not played audio. A cut after `reply_end` (TTS still draining) is not reported. Option: the provider posts the played prefix.
3. Device tools (`self.screen.set_bedside`, volume, …) become unreachable under `nointent`: "set bedside mode" by voice regresses. Recommendation: a service app tool `device_action` emitted as an SSE event that the provider runs through the device MCP client. This needs an orchestrator decision before the gate; until then it is a documented regression.

## 11. Tests and acceptance

- **Unit** (fake service SSE scripting deltas, heartbeats, delays, drops, 503/401): each `reply_end` reason; heartbeat yields; abort closes < 100 ms with no stale text queued; one resume then fallback, nothing in history; breaker; switch-race wait; `VOICE_LLM` render; first-clause segmenter (en, zh, mixed, abbreviations, "On it.", split deltas); metrics line; no cue after FIRST.
- **Sim stack** (`server/sim`, with the real agent-service once deployed): quick ask; an HA read; a long task with a steer "how is it going?"; barge-in mid-reply; service stopped → fallback within 2 s; rollback to `anthropic` with one turn.
- **Acceptance** (DESIGN row as reworded by decision 22): a ~5 min voiceless task started by voice keeps running after the sim device disconnects at `reply_end`; on reconnect its result arrives through WHEN_IDLE (snapshot + spoken delivery). Plus the section 9 numbers from 10 sim turns (TTS real, audio not played).
- **Milestone E2E** (38, at home): audible first audio, spoken steer and barge-in, cue interplay.
