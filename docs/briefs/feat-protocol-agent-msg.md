# feat-protocol-agent-msg — developer brief

Branch `feat/protocol-agent-msg`, 2026-10-07. Sources: `tasks.yml` (with the 2026-10-07 ack note), DESIGN.md M2 and ADR-11, decisions 31–36, UI report `weixue-agent-ui-v1.md` §3–§4.9 and §7.7, agent-service brief rev 3 §5 and §10. A firmware developer and a server developer work in parallel against this contract. The server runs in fixture mode until feat-agent-service exists.

## 1. Wire contract v1

```jsonc
// ── server → device: WebSocket text frame on the existing custom channel (CONFIG_RECEIVE_CUSTOM_MESSAGE=y)
{"type":"custom","payload":{
  "kind":"agent", "v":1,
  "seq":1739,                  // int64, monotonic per device (outbox gseq), never reused
  "ts":1791380000123,          // server epoch ms, used in logs only
  "chat_id":"c_7f3a", "task_id":"t_91b2",          // ≤ 36 ASCII each
  "state":"needs_approval",    // working|needs_input|needs_approval|unread|done|failed|cancelled|interrupted|stopped
  "title":"Living room",       // optional, ≤ 24 chars (UI main line)
  "summary":"Turn on living room lights",          // ≤ 60 code points, en or zh
  "lang":"en",                 // en|zh
  "progress":{"step":2,"total":5},                 // optional, working only; total may be null
  "card_id":"i_44c0",          // required when options is present (the interaction id); echoed back
  "options":[{"id":"approve","label":"Approve"},{"id":"deny","label":"Deny"}],  // ≤ 4; label ≤ 12 Latin / ≤ 4 CJK
  "risk":"reversible",         // needs_approval only: reversible|irreversible|money
  "timeout_s":300,             // seconds left at send time; the device counts from receipt; null = no timeout
  "delivery":"when_idle",      // interrupt|when_idle|silent (presentation tier)
  "speaks":true,               // a tts turn with ack:true for this item follows
  "playback":"uncertain",      // optional on unread|failed: played|cut_off|uncertain
  "seen":false,                // terminal states: whether a user ack is recorded
  "debug_reply":{"kind":"approve","option_id":"approve"}   // fixture only; honoured only by loopback builds
}}
{"type":"custom","payload":{"kind":"agent_sync","v":1,"seq":1745,"task_ids":["t_91b2","t_0c11"]}}
// ends a snapshot: the device drops tasks not listed

// ── server → device: tts frames of agent speech carry two extra fields
{"type":"tts","state":"start","session_id":"…","sentence_id":"9f2c…","ack":true}   // also on sentence_start and stop

// ── server → device: long-poll body (idle device, GET /weixue/proactive/poll → 200)
// request header  X-Weixue-Agent-Seq: <highest seq applied; 0 after boot = send a full snapshot>
{"pending":true,"speak":false,"urgent":false,"id":null,
 "agent":[<agent payload>, … ≤ 8, then agent_sync when this is a snapshot]}
// speak:true (with id) keeps today's behaviour: open a session and send weixue:proactive

// ── device → server: MCP notifications (SendMcpMessage in a session, SendDeviceEvent while idle)
{"jsonrpc":"2.0","method":"notifications/weixue/agent_response","params":{
  "v":1, "boot":2918841203, "seq":12,   // device counter per boot; server dedups on (device, boot, seq)
  "kind":"approve",       // approve|deny|answer|cancel|ack_seen|switch_chat|new_chat|shown
  "chat_id":"c_7f3a", "task_id":"t_91b2",
  "card_id":"i_44c0",     // approve|deny|answer
  "option_id":"approve",  // answer; optional on approve|deny
  "text":"…",             // answer only, ≤ 120 chars
  "ack_upto":1745,        // shown: highest server seq applied
  "via":"tap"}}           // tap|loopback (the device never judges voice; voice matching is server-side)
{"jsonrpc":"2.0","method":"notifications/weixue/played","params":{
  "v":1, "boot":2918841203, "seq":13, "sentence_id":"9f2c…",
  "complete":false, "cut_at_ms":1840, "reason":"wake"}}   // reason wake|tap|imu|mute|closed|superseded
// complete:true carries no cut fields

// ── limits
// agent frame ≤ 1024 B UTF-8 (the device drops anything > 2048 B and logs it); poll body ≤ 4096 B and ≤ 8 items;
// device → server ≤ 512 B. Unknown fields are ignored. An unknown kind or state, or v > 1, is dropped and logged, never acked.
```

**States.** `working`: a turn is in flight. `needs_input` and `needs_approval`: a card is open. `interrupted`: a new message interrupted the turn (decision 36 steer); it is transient and the task continues, normally with `working` next. `unread`: the task finished and the user has not acknowledged the result. `done`: finished and acknowledged; the device clears the complication. `failed`, `cancelled` (by the service or elsewhere) and `stopped` (this device's Stop, or the voice stop lexicon) are terminal and stay shown until `seen:true`. The device never sends a deny on timeout: the server and ha-mcp own expiry (decision 32). These names are the wire names. `weixue_agent_msg.py` is the only mapper, so agent-service §5's `needs_input→needs_clarification` rename is dropped.

**Versioning.** The device's `hello` adds `features.agent: 1`. Agent frames go only to sessions that advertise it, so neither patch 26's event channel nor older firmware receives one. A breaking change bumps `v` and the feature number together.

**Idempotency and ordering.** The server sends in `seq` order. The device applies an item only when its `seq` is higher than the last one stored for that `task_id`. A duplicate is acked again but not reapplied; an older item is ignored. Per-task marks let a snapshot replay each task's latest item under its original `seq`. Decisions use `decision_id = uuid5(device:boot:seq)`, so the first one wins (agent-service §5).

**Acks.** `shown` means the store applied the item; feat-ui-agent-states moves it to render time. Idle devices ack implicitly through the next poll's `X-Weixue-Agent-Seq`. `played` means the sentence's audio drained (`complete`) or was cut. `ack_seen` is the user ack, sent on a tap of the card or complication. agent-service, not the device, counts a follow-up question as a user ack.

**Busy device.** Cards are pushed at once and the device queues them visually by tier. Speech uses the proactive queue: WHEN_IDLE is non-urgent (quiet window in a session, or the poll answering `speak:true` once idle), INTERRUPT is `urgent`, SILENT is never spoken. Acceptance (DESIGN M2): a WHEN_IDLE item queued while the device is busy plays within 10 s of the device returning to idle. It is measured from the logged idle transition to that `sentence_id`'s `tts start`. Unacked frames are resent in-session after 3 s (up to 3 times), then through the snapshot on each later poll or session.

## 2. Firmware

Everything goes in the board directory, which `main/CMakeLists.txt` globs, so no CMake patch is needed:

- `weixue_agent_msg.h`: `Parse(const cJSON*) → std::optional<AgentMsg>`, limit checks, UTF-8-safe truncation.
- `weixue_agent_store.h`: pure C++ like `weixue_session.h`; at most 8 tasks, with the current task per chat. API: `Apply → applied|duplicate|stale|invalid`; `Sync(task_ids)`; `MarkSeen`; `Top()`, using the UI §3.1 priority (approval > input > failed > unread > working); and `deadline_ms`, set at receipt from `timeout_s`. Eviction removes the oldest seen terminal task first.
- `weixue_agent.{h,cc}`: ESP glue. Parses on the network task, applies on the main task via `Schedule`; `Subscribe(cb)` for the UI tasks; `SendResponse(kind, …)` for UI buttons; a RAM ack outbox (8 entries, retries at 1/3/9 s via `SendDeviceEvent`, lost on reboot, decision 35). Every apply logs `agent: chat=… task=… state=… seq=… tier=…`.
- `patches/30-application-agent-msg.patch`:
  - `custom` frames of kind `agent`/`agent_sync` go to `WeixueAgent` (no `SetChatMessage`); `tts` frames yield `sentence_id`/`ack`.
  - The poll reads `agent[]`/`speak` and sends `X-Weixue-Agent-Seq`; `websocket_protocol.cc` adds the `hello` feature.
- `patches/31-audio-played-frames.patch` (only if no existing hook fits): a played-frames counter in `AudioService`.
- Ack emission points:
  - **Complete:** `tts stop` for an `ack:true` sentence has arrived and the playback queue has drained (`IsIdle()`, checked on a 100 ms tick).
  - **Cut:** any of `AbortSpeaking` (wake word, IMU double-tap, touch), the mute path (fix-wave3 C1), the channel closing before the queue drains, or a new `tts start` with another `sentence_id`.
  - `cut_at_ms` is counted from the sentence's first output frame and must be within ±250 ms.
- **Loopback fixture mode:** a `WEIXUE_AGENT_LOOPBACK=1` build (`build.sh` compile definition) answers an item's `debug_reply` after 2 s through the real `SendResponse` path (`via:"loopback"`). Release builds ignore the field; the gate ends on a release build.
- Host tests `agent_msg_test.cc` and `agent_store_test.cc` link cJSON from `$WEIXUE_VENDOR/fw-work/*/managed_components/espressif__cjson`.

## 3. Server

- **`server/overlay/weixue_agent_msg.py`:** wire builders and validators, the limits, and the agent-service Delivery → wire mapping.
- **`server/overlay/weixue_agent_delivery.py`, the gateway:**
  - **Sources.** `GET /v1/deliveries/stream` (two-phase `lease_id` ack) when `AGENT_SERVICE_URL` is set; fixture injection at `POST /weixue/agent/messages` (`WEIXUE_PROACTIVE_TOKEN` bearer) always. It mirrors the latest item per task per device, with acks.
  - **Push and poll.** Pushes to `features.agent` sessions, builds poll-body batches and the snapshot (`agent_sync`) on session open or `X-Weixue-Agent-Seq: 0`, resends unacked items.
  - **Speech.** Spoken items go to `Proactive.submit` (`urgent = delivery == interrupt`, `device` set, `Idempotency-Key=<delivery_id>:<attempt>`), tagged so `speak()` sends `sentence_id` + `ack:true` and holds the session until `played` or 8 s after `tts stop`. No `played` within 30 s → `lost` → agent-service sends `unread` + `playback:"uncertain"` and never replays.
- **`weixue_proactive.py`:** poll-body `agent[]`, `speak:false` (200 when only cards are pending), the poll header, the delivery tag; hook calls only.
- **`server/overlay/weixue_patches.py`:**
  - Extend `log_device_events` so `agent_response` and `played` reach the gateway, with dedup on (device, boot, seq).
  - Wrap `send_tts_message` to add `sentence_id` and `ack`.
  - Record `features.agent` from `hello`.
- **Persistence.** The gateway never writes agent-service's database. Acks (`shown`, `played`, `cut_off`, `lost`, and `user_ack` from `ack_seen` plus `POST /chats/{id}/read`) go to `POST /v1/deliveries/{id}/ack`; agent-service writes `deliveries.playback` and a `delivery_acks` row (`delivery_id, device_id, boot, device_seq, outcome, sentence_id, cut_at_ms, reason, received_at`) that the monitor reads beside `connection_events`.
  - Responses map to the routes:

    | Device kind | agent-service route |
    |---|---|
    | `approve`, `deny`, `answer` (by `card_id`) | `approve`, `deny`, `answer` |
    | `cancel` | `interrupt{reason:"panel"}` |
    | `switch_chat`, `new_chat` | the chat routes |

  - Fixture mode logs acks (`agent ack …`) and serves them at `GET /weixue/agent/deliveries`.
- **`server/scripts/agent_fixture.py`:** sends scripted `agent` messages. Scenarios:
  - `states`: every state, card-only.
  - `approval` and `clarify`: with `debug_reply`.
  - `burst`: duplicates, out-of-order items, oversize items.
  - `snapshot`: device reboot.
  - `spoken`: audio, so home only.

  It prints acks per item and logs to `docs/logs/`.
- **Tests:** `test_agent_msg.py`, `test_agent_delivery.py`: limits, mapping, dedup, ordering, poll batching, snapshot, resend, 30 s lost, played/cut outcomes; proactive tests stay green.

## 4. Test plan and end-to-end gate (office mode)

No audio in office mode. Hold the device lock from flash to the end of the test, and rebase the server branch onto `main` before any live deploy so `wg` stays. Save serial capture (`firmware/tools/serial_log.py`) and `docker logs xiaozhi-esp32-server` as `docs/logs/feat-protocol-agent-msg-*.log`.

1. Firmware `run.sh` and server pytest pass.
2. Flash the loopback build and run `agent_fixture.py states` with every item `speaks:false`, using all three tiers. Each state is applied once, with its tier, through both the poll body (idle) and an open session. tasks.yml needs 4 states recorded on the device; all 9 are expected.
3. `approval` and `clarify` scenarios: loopback `approve`/`answer` with `card_id` reach the server log and the fixture's ack view; `ack_seen` on `unread` produces `user_ack`.
4. `burst`: duplicates are re-acked and not reapplied, stale items are ignored, oversize items are dropped with a log line, and voice still works on the next wake (log only).
5. `snapshot`: reset the board. The first poll carries `X-Weixue-Agent-Seq: 0`, the snapshot plus `agent_sync` rebuild the store, and dropped tasks disappear.
6. Busy → idle timing for speech, run with the simulated device (no audio): ≤ 10 s.
7. Flash the release build.

Home audio gate (feat-voice-integration window): spoken WHEN_IDLE/INTERRUPT, the 10 s busy→idle acceptance on hardware, `played complete`, `cut_off` by wake word and IMU double-tap (`cut_at_ms` ±250 ms of serial timestamps), and gateway restart after unacked speech → `playback:"uncertain"`, no repeat.

## 5. Dependencies, merge order, files

feat-agent-service is needed only for the live `/v1/deliveries` source; feat-ui-panel is merged.

**fix-wave3-firmware** (in review, audio deferred) adds patches 23–28 (`application.cc` poll task, device events, mute) and changes `monitor_panel.*`, `face_display.*`, `weixue_session.h` and `tests/run.sh`. Firmware work starts from its tip (589e7d7) so patch 30 sits on 23–28, and fix-wave3-firmware merges first. If it changes before merging, regenerate patch 30, rebuild and repeat the on-device smoke test. This task leaves `monitor_panel.*`, `face_display.*`, `weixue_session.h` and the IMU files alone: UI wiring is feat-ui-agent-states, which merges after it.

**fix-wave5-server** has uncommitted changes to `weixue_proactive.py` and `weixue_patches.py`. The server side rebases after it merges, and its edits to both files stay to small hook calls; logic lives in the two new modules.

Firmware files touched:
- New, in `firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/`: `weixue_agent_msg.h`, `weixue_agent_store.h`, `weixue_agent.h`, `weixue_agent.cc`.
- New patches: `firmware/patches/30-application-agent-msg.patch` (`main/application.cc`, `main/application.h`, `main/protocols/websocket_protocol.cc`) and, only if needed, `firmware/patches/31-audio-played-frames.patch` (`main/audio/audio_service.{h,cc}`).
- New tests: `firmware/tests/agent_msg_test.cc`, `firmware/tests/agent_store_test.cc`.
- Edited: `firmware/tests/run.sh` (two test names) and `firmware/build.sh` (the `WEIXUE_AGENT_LOOPBACK` definition).

Server files touched: new `server/overlay/weixue_agent_msg.py`, `server/overlay/weixue_agent_delivery.py`, `server/scripts/agent_fixture.py`, `server/tests/test_agent_msg.py`, `server/tests/test_agent_delivery.py`, `docs/agent-protocol.md` (this contract); edited `server/overlay/weixue_proactive.py`, `server/overlay/weixue_patches.py`, `server/overlay/weixue_main.py` (routes), `docs/proactive.md` (poll body).

The spec names the items deferred to the home audio gate (§4).
