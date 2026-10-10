# Agent protocol v1.1 (feat-protocol-agent-msg, fix-agent-msg-delivery)

Server → device task-state messages and response receipts, device → server responses and playback acks. Contract of DESIGN.md M2 and ADR-11; brief `docs/briefs/feat-protocol-agent-msg.md`. v1.1 adds the `agent_ack` receipt and the poll body cap; `v` stays 1.

| Part | Where |
|---|---|
| Wire builders, validators, limits, agent-service mapping | `server/overlay/weixue_agent_msg.py` |
| Gateway: mirror, push, poll batches, snapshot, resend, speech, acks, routes | `server/overlay/weixue_agent_delivery.py` |
| Proactive hooks (poll body, speech hold, `sentence_id` tag) | `server/overlay/weixue_proactive.py` |
| Device parser, store, outbox, ESP glue | `firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/weixue_agent_msg.h`, `weixue_agent_store.h`, `weixue_agent_outbox.h`, `weixue_agent.{h,cc}` |
| Firmware hooks | `firmware/patches/30-application-agent-msg.patch`, `31-audio-played-frames.patch` |
| Fixture, simulated device | `server/scripts/agent_msg_fixture.py`, `server/scripts/agent_sim_device.py` |

## Wire

Server → device, on the WebSocket `custom` channel (`CONFIG_RECEIVE_CUSTOM_MESSAGE=y`) or in the poll body:

```jsonc
{"type":"custom","payload":{
  "kind":"agent", "v":1,
  "seq":1739,              // int64, monotonic per device, never reused
  "ts":1791380000123,      // server epoch ms, logs only
  "chat_id":"c_7f3a", "task_id":"t_91b2",       // ≤ 36 printable ASCII, no '"' '\' '#' '/' '?'; same for card_id
  "state":"needs_approval",// working|needs_input|needs_approval|unread|done|failed|cancelled|interrupted|stopped
  "title":"Living room",   // optional, ≤ 24 chars
  "summary":"Turn on living room lights",       // ≤ 60 code points
  "lang":"en",             // en|zh
  "progress":{"step":2,"total":5},              // working only; total may be null
  "card_id":"i_44c0",      // required with options; echoed in responses
  "options":[{"id":"approve","label":"Approve"},{"id":"deny","label":"Deny"}],  // ≤ 4, label ≤ 12 Latin / ≤ 4 CJK
  "risk":"reversible",     // needs_approval only: reversible|irreversible|money
  "timeout_s":300,         // seconds left at send time, counted from receipt; null = none
  "delivery":"when_idle",  // interrupt|when_idle|silent
  "speaks":true,           // an ack:true tts turn for this item follows
  "playback":"uncertain",  // unread|failed only: played|cut_off|uncertain
  "seen":false,            // terminal and unread states
  "debug_reply":{"kind":"approve","option_id":"approve"}   // fixture only; loopback builds only
}}
{"type":"custom","payload":{"kind":"agent_sync","v":1,"seq":1745,"task_ids":["t_91b2","t_0c11"]}}
```

`agent_sync` ends a snapshot: the device drops every task not listed. Agent speech `tts` frames (`start`, `sentence_start`, `stop`) carry `"sentence_id":"…","ack":true`.

Receipt (v1.1), on the session the device's `agent_response` arrived on, for every response kind except `shown`, and again for every repeat of the same (`boot`, `seq`):

```jsonc
{"type":"custom","payload":{"kind":"agent_ack","v":1,
  "boot":2918841203,"seq":12,          // the response's boot and seq
  "response":"approve","card_id":"i_44c0",
  "result":"accepted",                 // accepted|duplicate|stale
  "decided":"approve"}}                // duplicate only: the card's first decision
```

`accepted`: recorded (and forwarded in live mode). `duplicate`: the card already had a decision (first wins), so the server holds one. `stale`: not the task's current card; dropped.

Poll body (idle device, `GET /weixue/proactive/poll` with header `X-Weixue-Agent-Seq: <highest applied seq>`; `0` after boot asks for a full snapshot):

```jsonc
{"pending":true,"speak":false,"urgent":false,"id":null,"lease":null,
 "agent":[<agent payload>, … ≤ 2 entries and ≤ 1200 B, agent_sync last when a snapshot ends]}
```

The gateway sends at most 2 entries (`agent_sync` counts) and 1200 B per body, so headers and body fit one TCP segment of the device (MSS 1440 B); a single larger item goes alone. Items continue on the next poll from its header. A snapshot is sent page by page; when the next poll's header is below the highest seq of the last page (the page or its `agent_sync` was not applied), the page goes again, at most 3 times in a row.

`speak:true` (with `id` and `lease`) opens a session and sends `weixue:proactive` as before. A poll without the header gets no `agent` array.

Device → server, as MCP notifications (`SendDeviceEvent`):

```jsonc
{"jsonrpc":"2.0","method":"notifications/weixue/agent_response","params":{
  "v":1,"boot":2918841203,"seq":12,     // per-boot counter; server dedups on (device, boot, seq)
  "kind":"approve",                     // approve|deny|answer|cancel|ack_seen|switch_chat|new_chat|shown
  "chat_id":"c_7f3a","task_id":"t_91b2",
  "card_id":"i_44c0",                   // approve|deny|answer
  "option_id":"approve",                // answer; optional on approve|deny
  "text":"…",                           // answer only, ≤ 120 chars
  "ack_upto":1745,                      // shown only
  "via":"tap"}}                         // tap|loopback
{"jsonrpc":"2.0","method":"notifications/weixue/played","params":{
  "v":1,"boot":2918841203,"seq":13,"sentence_id":"9f2c…",
  "complete":false,"cut_at_ms":1840,"reason":"wake"}}   // wake|tap|imu|mute|closed|superseded
```

Limits: agent item ≤ 1024 B (the device drops > 2048 B and logs it); the device accepts poll bodies up to 4096 B and 8 items; device → server ≤ 512 B. Unknown fields are ignored; an unknown kind or state, or `v` > 1, is dropped and logged, never acked.

## Versioning

`hello` carries `features.agent: 1`. Agent frames go only to verified sessions that advertise it. A breaking change bumps `v` and the feature number together.

## Device store

At most 8 tasks; the current task per chat follows its latest item. An item applies only when its `seq` is above the task's stored `seq` (`applied`); an equal `seq` is `duplicate` (acked, not reapplied), a lower one `stale` (ignored). `Top()` priority: approval > input > failed > unread > working, newest first among equals. `done` clears the task; `failed`, `cancelled` and `stopped` stay until seen. Eviction order: oldest seen terminal task, oldest terminal task, oldest working task, oldest task that is not an open card. An open card (`needs_approval`, `needs_input`) is never evicted: a new task arriving while all 8 slots hold open cards is refused (`full`, logged, not acked), and the gateway drops it first (ack `dropped`). The device never denies on timeout.

`X-Weixue-Agent-Seq` is the highest seq the store applied (or a sync's seq); the poll task sends its next poll only after the main task applied the previous body, so an item that is refused or invalid is never counted.

## Acks

| Ack | Sent | Gateway outcome |
|---|---|---|
| `shown` (`ack_upto`) | in session, 200 ms after the UI drew the applied store; idle devices ack through the next poll's header | `shown` |
| `played complete` | `tts stop` arrived and playback drained | `played` |
| `played` cut | abort (wake, tap, IMU), mute, channel close before drain, new `tts start` with another `sentence_id`, `tts start` refused while muted | `cut_off` |
| none within 30 s of playback end | — | `lost` |
| `ack_seen` | tap on the card or complication | `user_ack` |

The device outbox (`weixue_agent_outbox.h`) holds 8 entries in RAM, sends one at a time and retries a send that did not reach the transport after 1, 3 and 9 s; it is lost on reboot. A response other than `shown` stays until its `agent_ack` arrives: without one 2 s after it reached the transport it is sent again (same `boot` and `seq`) after 1, 3 and 9 s, and it is given up after the fourth attempt. While it waits, later entries go out. A response sent on the device's own short session keeps that session open until the receipt arrives (at most 2 s). A 200 poll whose body did not arrive whole (shorter than its Content-Length, or not a JSON object) is a failed poll: the device polls again after 1 s and opens no session.

## Gateway

- **Sources.** Fixture injection `POST /weixue/agent/messages` (bearer `WEIXUE_PROACTIVE_TOKEN`) always; `GET /v1/deliveries/stream` of agent-service when `AGENT_SERVICE_URL` is set.
- **Mirror.** The latest item per task per device (8 tasks, same eviction order as the device).
- **Push.** Items go at once to the newest live agent session; unacked items are resent after 3 s, up to 4 sends, then a snapshot is due.
- **Poll.** Batches unacked items within the poll cap; a seq-0 poll or a drop makes the next poll a snapshot (mirror then `agent_sync`). The held poll answers as soon as agent items are pending; a held poll that a newer poll from the device replaced answers `204` and takes no items. An item polled 4 times without the header covering it goes to a snapshot.
- **Speech.** A spoken item becomes a proactive `announce` (`urgent` = `interrupt`, `Idempotency-Key` `<delivery_id>:<attempt>`), so WHEN_IDLE speech waits for the device to be idle. Its tts frames carry `sentence_id` and `ack:true`, and the session is held until `played`, 8 s after `tts stop`, or close. `failed` before audio is resubmitted (3 attempts); `expired` is `dropped`.
- **Responses.** `approve`/`deny`/`answer` whose `card_id` is not the mirrored task's current card are dropped (`… stale … dropped`, receipt `stale`); a task the gateway does not mirror is forwarded. One decision per card: the first `approve`/`deny`/`answer` for a `card_id` (per device, last 1024 cards) wins, and any later one, a device retry with a new `seq` included, is dropped (`… duplicate decision … dropped`, receipt `duplicate`). A repeat of a (`boot`, `seq`) already seen is not processed again and gets that event's receipt (`agent receipt … result=…`).
- **Closing window.** A session the device opened for a proactive invocation that no longer has a lease (`proactive session opened with no current lease for it; closing once the device is quiet`) hears no voice and stays open until no agent notification arrived for 2 s (at most 8 s), so responses and `shown` acks the device sends in it are handled and receipted; then it closes. The rest are logged as `agent response …`; in live mode posted to agent-service with `Idempotency-Key = uuid5(device:boot:seq)`: `approve`/`deny`/`answer` to the chat's routes (chat id URL-quoted), `cancel` to `interrupt{reason:"panel"}`, `switch_chat`/`new_chat`/`ack_seen` to the chat routes.
- **Persistence.** Acks are logged (`agent ack <outcome> …`), kept at `GET /weixue/agent/deliveries`, and in live mode posted to `POST /v1/deliveries/{id}/ack`; the gateway writes no database.

## Fixture

```bash
WEIXUE_PROACTIVE_TOKEN=... server/scripts/agent_msg_fixture.py --base http://<host>:8003 --device <mac> <scenario>
```

Scenarios: `states`, `approval`, `clarify`, `unread`, `burst`, `seed` + `drop`, `busy`, `spoken`, `ui` (the device UI tour across three chats), `eight` (8 full-size tasks: paged poll bodies, the snapshot after a board reset), `closing` (an announcement plus an approval answered by loopback while the server closes the session), `view`. `agent_sim_device.py --weak-link 1440` cuts poll responses after one TCP segment of the device, `--old-firmware` treats such a body as pending as v1 firmware did, `--invoke-at S` opens a session without a current lease. A `WEIXUE_AGENT_LOOPBACK=1 firmware/build.sh` build answers `debug_reply` 2 s after an item applies (`via:"loopback"`), plays at output volume 0 and keeps the bedside page off (RAM `day` hint) for that boot; release builds ignore `debug_reply`. Once the device answered an item, snapshots, polls and resends carry it without `debug_reply`, so a rebooted loopback build does not answer twice. `server/scripts/agent_sim_device.py` plays the device side without hardware.
