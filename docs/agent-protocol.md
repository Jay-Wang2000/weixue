# Agent protocol v1.2 (feat-protocol-agent-msg, fix-agent-msg-delivery, feat-ui-sessions)

Server → device task-state messages, the chat list and response receipts, device → server responses and playback acks. Contract of DESIGN.md M2 and ADR-11; brief `docs/briefs/feat-protocol-agent-msg.md`. v1.1 adds the `agent_ack` receipt and the poll body cap; v1.2 adds the `agent_chats` chat list for devices at feature level 2; `v` stays 1.

| Part | Where |
|---|---|
| Wire builders, validators, limits, agent-service mapping | `server/overlay/weixue_agent_msg.py` |
| Gateway: mirror, push, poll batches, snapshot, resend, speech, acks, routes | `server/overlay/weixue_agent_delivery.py` |
| Proactive hooks (poll body, speech hold, `sentence_id` tag) | `server/overlay/weixue_proactive.py` |
| Device parser, store, outbox, ESP glue | `firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/weixue_agent_msg.h`, `weixue_agent_store.h`, `weixue_agent_outbox.h`, `weixue_agent.{h,cc}` |
| Firmware hooks | `firmware/patches/30-application-agent-msg.patch`, `31-audio-played-frames.patch` |
| Fixture, simulated device, chat switching end to end | `server/scripts/agent_msg_fixture.py`, `server/scripts/agent_sim_device.py`, `server/scripts/agent_chats_e2e.py` |
| Firmware feature level (hello, poll header) | `firmware/patches/35-agent-chats-feature.patch` |

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
{"type":"custom","payload":{"kind":"agent_chats","v":1,"seq":1746,"ts":1791380000456,   // v1.2, level-2 devices only
  "current":"cht_01J9…",   // omitted when the device has no current chat
  "chats":[{"id":"cht_01J9…","title":"Living room","state":"needs_you","age_s":120},   // display order, current first
           {"id":"cht_01J8…","state":"idle","age_s":86400}]}}                          // no title: untitled
```

`agent_sync` ends a snapshot: the device drops every task not listed. `agent_chats` replaces the device's chat list (the newest seq wins; a task snapshot keeps it): at most 12 entries, the payload ≤ 1024 B (the gateway leaves out the oldest chats that do not fit, never the current one), `title` ≤ 24 chars, `state` `idle|working|needs_you|unread|failed` (unknown reads `idle`), `age_s` seconds since the chat's last activity at send time. Agent speech `tts` frames (`start`, `sentence_start`, `stop`) carry `"sentence_id":"…","ack":true`.

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

`hello` carries `features.agent: <level>` and polls carry `X-Weixue-Agent-Feature: <level>` (absent: level 1). Level 1 is v1.1; level 2 also applies `agent_chats`. Agent frames go only to verified sessions that advertise `features.agent`; `agent_chats` only to a device whose newest session or last poll gave level 2 (until then the gateway holds the newest list; a v1.1 device's chat deliveries are acked `dropped`). An additive message raises the level only; a breaking change bumps `v` and the level together.

## Device store

At most 8 tasks; the current task per chat follows its latest item. An item applies only when its `seq` is above the task's stored `seq` (`applied`); an equal `seq` is `duplicate` (acked, not reapplied), a lower one `stale` (ignored). `Top()` priority: approval > input > failed > unread > working, newest first among equals. `done` clears the task; `failed`, `cancelled` and `stopped` stay until seen. Eviction order: oldest seen terminal task, oldest terminal task, oldest working task, oldest task that is not an open card. An open card (`needs_approval`, `needs_input`) is never evicted: a new task arriving while all 8 slots hold open cards is refused (`full`, logged, not acked), and the gateway drops it first (ack `dropped`). The device never denies on timeout.

The chat list is stored apart from the tasks; its seq counts toward the applied seq. `X-Weixue-Agent-Seq` is the highest seq the store applied (or a sync's or a chat list's seq); the poll task sends its next poll only after the main task applied the previous body, so an item that is refused or invalid is never counted.

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

- **Sources.** Fixture injection `POST /weixue/agent/messages` (bearer `WEIXUE_PROACTIVE_TOKEN`) always; `GET /v1/deliveries/stream` of agent-service when `AGENT_SERVICE_URL` is set, opened on the device's first agent session or agent poll.
- **Chats.** The device's chat list comes from agent-service's chat index: `GET /v1/chats?device_id` when the delivery stream (re)connects and after every switch, and the stream's `chats` deliveries. Titles are the service's (`set_chat_title`, else the first utterance); a chat with the placeholder title takes the title (else summary) of its newest mirrored task, else none. A list equal to the last one sent (ids, titles, states, current) is not sent again (its delivery is acked `shown`). The newest list is part of every snapshot (before `agent_sync`) and is acked like an item (`shown` when the header or `ack_upto` covers its seq).
- **Mirror.** The latest item per task per device (8 tasks, same eviction order as the device).
- **Push.** Items go at once to the newest live agent session; unacked items are resent after 3 s, up to 4 sends, then a snapshot is due.
- **Poll.** Batches unacked items within the poll cap; a seq-0 poll or a drop makes the next poll a snapshot (mirror then `agent_sync`). The held poll answers as soon as agent items are pending; a held poll that a newer poll from the device replaced answers `204` and takes no items. An item polled 4 times without the header covering it goes to a snapshot.
- **Speech.** A spoken item becomes a proactive `announce` (`urgent` = `interrupt`, `Idempotency-Key` `<delivery_id>:<attempt>`), so WHEN_IDLE speech waits for the device to be idle. Its tts frames carry `sentence_id` and `ack:true`, and the session is held until `played`, 8 s after `tts stop`, or close. `failed` before audio is resubmitted (3 attempts); `expired` is `dropped`.
- **Chat switch.** `switch_chat` posts `POST /v1/chats/{id}/switch`, `new_chat` posts `POST /v1/chats` (created and current); the receipt waits for the service (at most 3 s): `accepted` on 2xx, else `stale` (the device shows `Couldn't switch`); a repeat while the post runs gets no receipt. Then the refreshed list goes to the device. The provider's next turn posts to `chats/current` after waiting up to 1 s for a switch still in flight.
- **Responses.** `approve`/`deny`/`answer` whose `card_id` is not the mirrored task's current card are dropped (`… stale … dropped`, receipt `stale`); a task the gateway does not mirror is forwarded. One decision per card: the first `approve`/`deny`/`answer` for a `card_id` (per device, last 1024 cards) wins, and any later one, a device retry with a new `seq` included, is dropped (`… duplicate decision … dropped`, receipt `duplicate`). A repeat of a (`boot`, `seq`) already seen is not processed again and gets that event's receipt (`agent receipt … result=…`).
- **Closing window.** A session the device opened for a proactive invocation that no longer has a lease (`proactive session opened with no current lease for it; closing once the device is quiet`) hears no voice and stays open until no agent notification arrived for 2 s (at most 8 s), so responses and `shown` acks the device sends in it are handled and receipted; then it closes. The rest are logged as `agent response …`; in live mode posted to agent-service with `Idempotency-Key = uuid5(device:boot:seq)`: `approve`/`deny`/`answer` to the chat's routes (chat id URL-quoted), `cancel` to `interrupt{reason:"panel"}`, `switch_chat`/`new_chat`/`ack_seen` to the chat routes.
- **Persistence.** Acks are logged (`agent ack <outcome> …`), kept at `GET /weixue/agent/deliveries`, and in live mode posted to `POST /v1/deliveries/{id}/ack`; the gateway writes no database.

## Service deliveries (agent-service outbox)

Offers on `GET /v1/deliveries/stream` have kind `agent` (task cards and notices) or `chats`; acks go to `POST /v1/deliveries/{id}/ack`.

- **Notices.** `notify_user` is an `agent` delivery of its own card: `task_id` = `ntf_<ulid>` (no task row), `state: unread`, `title` `Notice` / `通知`, `summary` its text (≤ 60), `speak` its text, tier `INTERRUPT` (`urgent`) or `WHEN_IDLE`, `expires_at` 1 h; the payload adds `notice: true` and `origin_task_id`. The gateway carries it as any task item (mirror, speech through the proactive queue with `urgent` for INTERRUPT, playback and `ack_seen` acks); a tap on it clears it.
- **Lease and attempt.** Every ack carries the offer's `lease_id` and may carry its `attempt`; one that is not the current offer's is `409 stale_lease`, and the gateway forgets that offer. A revoked stream's offers return to pending without a lease.
- **Retry owner.** The service: `deferred` (only before any audio) returns the delivery to pending with `attempt + 1`; the next offer has a new lease and `Idempotency-Key` `<delivery_id>:<attempt>`. The gateway does not resubmit an offer it acked `deferred`.
- **Monotonic acks.** `accepted` only from `offered` (a repeat is idempotent; otherwise `409`, and the speech is not queued); `spoken` from `offered|handed_off`; `played` and `cut_off` from `offered|handed_off|uncertain` (a late `played` upgrades an uncertain card); `lost` from `offered|handed_off`; `dropped` from `pending|offered|handed_off`; `shown` from `pending|offered`. Any other ack is recorded and changes nothing (`200`, `detail: ignored in <state>`); `deferred` after playback evidence is `409`.
- **Restart.** Speech that was offered or handed off without `played|cut_off` is never offered again: its card goes unread with `playback: uncertain`. Unspoken offers return to pending.

## Device tools (ADR-13)

The agent backend never operates the device's physical inputs (touch, BOOT button, gestures) and never answers an approval or clarify card itself: card responses come only from a person at the device. Device MCP tools have two classes:

- **agent-visible**: `self.get_device_status`, `self.audio_speaker.set_volume`, `self.screen.set_brightness`, `self.screen.set_theme`, `self.screen.set_bedside`, `self.imu.get_state`. Only these may reach the model, through the xiaozhi device-MCP client or the gateway's `device_action`.
- **operator-only**: every other name (`self.system.reconfigure_wifi`, the upstream user-only tools such as `self.reboot` and `self.upgrade_firmware`, camera, lamp, any later tool). Never listed or callable on the MCP channel; input injection exists only on the diag build's USB serial console.

The device enforces the classes (`docs/firmware.md`, Device MCP tools). The server is the second line: `server/overlay/weixue_device_tools.py` drops a non-agent-visible tool from the device's `tools/list` before the model's function list is built (`device tool '<name>' dropped: not agent-visible`) and refuses a `call_mcp_tool` for one without sending it (`device tool call '<name>' refused: not agent-visible`). A gateway `device_action {tool, args}` runs through that same client call and checks `weixue_device_tools.is_agent_visible(tool)`.

## Fixture

```bash
WEIXUE_PROACTIVE_TOKEN=... server/scripts/agent_msg_fixture.py --base http://<host>:8003 --device <mac> <scenario>
```

Scenarios: `states`, `approval`, `clarify`, `unread`, `burst`, `seed` + `drop`, `busy`, `spoken`, `ui` (the device UI tour across three chats), `chats` (a raw `agent_chats` of five chats with the `ui` prefix's chat ids), `eight` (8 full-size tasks: paged poll bodies, the snapshot after a board reset), `closing` (an announcement plus an approval answered by loopback while the server closes the session), `view`. `agent_sim_device.py --weak-link 1440` cuts poll responses after one TCP segment of the device, `--old-firmware` treats such a body as pending as v1 firmware did, `--invoke-at S` opens a session without a current lease. A `WEIXUE_AGENT_LOOPBACK=1 firmware/build.sh` build answers `debug_reply` 2 s after an item applies (`via:"loopback"`), plays at output volume 0 and keeps the bedside page off (RAM `day` hint) for that boot; release builds ignore `debug_reply`. Once the device answered an item, snapshots, polls and resends carry it without `debug_reply`, so a rebooted loopback build does not answer twice. `server/scripts/agent_sim_device.py` plays the device side without hardware (`--level 1` as v1.1 firmware). `server/scripts/agent_chats_e2e.py` runs the chat-switching acceptance on the sim stack (`server/sim/docker-compose.agent.yml`): `new_chat` until five chats exist, ten `switch_chat`s each followed by a voice turn that must land in the selected chat (provider `turn_latency chat=` and an `accepted` event with the turn's trace id in that chat's events), and with `--restart <container>` the chat index, current chat and device list unchanged after an agent-service restart.
