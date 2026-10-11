# Agent protocol v1.3 (feat-protocol-agent-msg, fix-agent-msg-delivery, feat-ui-sessions, fix-m2-review-gateway, feat-panel-current-tool)

Server → device task-state messages, the chat list and response receipts, device → server responses and playback acks. Contract of DESIGN.md M2 and ADR-11; brief `docs/briefs/feat-protocol-agent-msg.md`. v1.1 adds the `agent_ack` receipt and the poll body cap; v1.2 adds the `agent_chats` chat list for devices at feature level 2; v1.3 adds `progress.activity`; `v` stays 1.

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
  "progress":{"step":2,"total":5,"activity":"Searching…"},  // working only; step or activity; total may be null
  "card_id":"i_44c0",      // required with options; echoed in responses
  "options":[{"id":"approve","label":"Approve"},{"id":"deny","label":"Deny"}],  // ≤ 4, label ≤ 12 Latin / ≤ 4 CJK
  "risk":"reversible",     // needs_approval only: reversible|irreversible|money
  "timeout_s":300,         // seconds left at send time, counted from receipt; null = none
  "delivery":"when_idle",  // interrupt|when_idle|silent
  "speaks":true,           // an ack:true tts turn for this item follows
  "playback":"uncertain",  // unread|failed only: played|cut_off|uncertain
  "seen":false             // terminal and unread states
}}
{"type":"custom","payload":{"kind":"agent_sync","v":1,"seq":1745,"task_ids":["t_91b2","t_0c11"]}}
{"type":"custom","payload":{"kind":"agent_chats","v":1,"seq":1746,"ts":1791380000456,   // v1.2, level-2 devices only
  "current":"cht_01J9…",   // omitted when the device has no current chat
  "chats":[{"id":"cht_01J9…","title":"Living room","state":"needs_you","age_s":120},   // display order, current first
           {"id":"cht_01J8…","state":"idle","age_s":86400}]}}                          // no title: untitled
```

A `needs_input` item is a question card: `summary` is the question and `options` holds 2-4 choices, shown as the card's buttons; agent-service never sends a question without options (decision 67). The device still renders an option-less question with Later for items stored before that rule. A spoken reply to the card answers it in the user's own words through the normal voice path when it names an option or is a short answer (agent-service, `ask_user`); the gateway never delivers a `needs_input` or `needs_approval` card `silent` (a SILENT offer becomes `when_idle` without speech), and the device opens a card whose open item is raised from `silent` to `when_idle` or `interrupt` without a second earcon or flash.

`progress.activity` (v1.3, ≤ 24 chars, the chat's language) is what the agent is doing now (Service deliveries, Activity); a working item without it shows the state word. `progress` without `step` carries only the activity. A device that does not know the field ignores it (unknown fields are ignored).

`agent_sync` ends a snapshot: the device drops every task not listed. `agent_chats` replaces the device's chat list (the newest seq wins; a task snapshot keeps it): at most 12 entries, the payload ≤ 1024 B (the gateway leaves out the oldest chats that do not fit, never the current one), `title` ≤ 24 chars, `state` `idle|working|needs_you|unread|failed` (unknown reads `idle`), `age_s` seconds since the chat's last activity at send time. Agent speech `tts` frames (`start`, `sentence_start`, `stop`) carry `"sentence_id":"…","ack":true`.

Receipt (v1.1), on the session the device's `agent_response` arrived on, for every response kind except `shown`, and again for every repeat of the same (`boot`, `seq`):

```jsonc
{"type":"custom","payload":{"kind":"agent_ack","v":1,
  "boot":2918841203,"seq":12,          // the response's boot and seq
  "response":"approve","card_id":"i_44c0",
  "result":"accepted",                 // accepted|duplicate|stale
  "decided":"approve"}}                // duplicate only: the card's first decision
```

`accepted`: recorded; in live mode only once agent-service answered the decision's post with 2xx. `duplicate`: the card already had a decision (first wins), so the server holds one. `stale`: not the task's current card, or (live mode) agent-service refused the decision or did not answer within 12 s; the device keeps the card open (`Not sent · tap to retry`).

Poll body (idle device, `GET /weixue/proactive/poll` with header `X-Weixue-Agent-Seq: <highest applied seq>`; `0` after boot asks for a full snapshot):

```jsonc
{"pending":true,"speak":false,"urgent":false,"id":null,"lease":null,
 "agent":[<agent payload>, … ≤ 2 entries and ≤ 1200 B, agent_sync last when a snapshot ends]}
```

The gateway sends at most 2 entries (`agent_sync` counts) and 1200 B per body, so headers and body fit one TCP segment of the device (MSS 1440 B); a single larger item goes alone. Items continue on the next poll from its header. A snapshot is sent page by page; when the next poll's header is below the highest seq of the last page (the page or its `agent_sync` was not applied), the page goes again, at most 3 times in a row.

`speak:true` (with `id` and `lease`) opens a session that sends `weixue:proactive:<id>:<lease>` (level 2; level 1: the bare `weixue:proactive`); a level-2 session's bare marker is rejected (`docs/proactive.md`). A poll without the header gets no `agent` array.

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
  "via":"tap"}}                         // tap (the only value)
{"jsonrpc":"2.0","method":"notifications/weixue/played","params":{
  "v":1,"boot":2918841203,"seq":13,"sentence_id":"9f2c…",
  "complete":false,"cut_at_ms":1840,"reason":"wake"}}   // wake|tap|imu|mute|closed|superseded
```

Limits: agent item ≤ 1024 B (the device drops > 2048 B and logs it); the device accepts poll bodies up to 4096 B and 8 items; device → server ≤ 512 B. Unknown fields are ignored; an unknown kind or state, or `v` > 1, is dropped and logged, never acked.

No server message answers a card: approve, deny, answer and ack_seen leave the device only from a tap on its card or complication (ADR-13). The gateway refuses items with fields outside this schema (`debug_reply` included) and responses with a `via` other than `tap`.

## Versioning

`hello` carries `features.agent: <level>` and polls carry `X-Weixue-Agent-Feature: <level>` (absent: level 1). Level 1 is v1.1; level 2 also applies `agent_chats` and names the poll answer's `id` and `lease` in its proactive invocation. Agent frames go only to verified sessions that advertise `features.agent`; `agent_chats` only to a device whose newest session or last poll gave level 2 (until then the gateway holds the newest list; a v1.1 device's chat deliveries are acked `dropped`). An additive message raises the level only; a breaking change bumps `v` and the level together.

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
- **Restore.** In live mode, before the first delivery stream of a device in this gateway process, `GET /v1/devices/{id}/snapshot` restores the mirror (its `agent` cards, never their speech) and the chat list (`agent restore device=… N cards from the agent-service snapshot`). Until then no snapshot or `agent_sync` goes to the device (sessions and seq-0 polls wait), and without the snapshot (no 200 within 4 s) the stream is not opened (retried with the stream's backoff). A restored card keeps the wire seq the device applied when the state file has it, so the device takes it as a duplicate. Ordering: the snapshot is the state at the moment it was read; offers on the stream that opens afterwards replace a restored card of the same task unless their `gseq` is lower, in which case they are not sent (acked `dropped`, or only spoken).
- **Approval tier.** A `needs_approval` item is never `silent` on the wire: an offer tiered `SILENT` (its cue was spoken inline in the reply) goes out `when_idle` with `speaks:false`; `INTERRUPT` stays `interrupt`.
- **Boot snapshot.** The snapshot a seq-0 poll asks for (on a poll or the next agent session) gives the mirrored tasks new seqs in this order: open approval cards, then question cards, then the rest, each in the order they were queued (`agent boot snapshot device=… order …`); a mirror already in that order keeps its seqs.
- **Chats.** The device's chat list comes from agent-service's chat index: `GET /v1/chats?device_id` when the delivery stream (re)connects and after every switch, and the stream's `chats` deliveries. Titles are the service's (`set_chat_title`, else the first utterance); a chat with the placeholder title takes the title (else summary) of its newest mirrored task, else none. A list equal to the last one sent (ids, titles, states, current) is not sent again (its delivery is acked `shown`). The newest list is part of every snapshot (before `agent_sync`) and is acked like an item (`shown` when the header or `ack_upto` covers its seq).
- **Mirror.** The latest item per task per device (8 tasks, same eviction order as the device).
- **Push.** Items go at once to the newest live agent session; unacked items are resent after 3 s, up to 4 sends, then a snapshot is due.
- **Poll.** Batches unacked items within the poll cap; a seq-0 poll or a drop makes the next poll a snapshot (mirror then `agent_sync`). The held poll answers as soon as agent items are pending; a held poll that a newer poll from the device replaced answers `204` and takes no items. An item polled 4 times without the header covering it goes to a snapshot.
- **Speech.** A spoken item becomes a proactive `announce` (`urgent` = `interrupt`, `Idempotency-Key` `<delivery_id>:<attempt>` with the offer's `attempt`), so WHEN_IDLE speech waits for the device to be idle. In live mode the `accepted` ack (with the proactive id chosen beforehand) is posted first, retried for 8 s, and the message is queued only after agent-service answered 2xx; a 4xx leaves the attempt unplayed (`refused the lease`), no answer too (`did not record the hand-off`; the service's lease expiry offers it again). A delivery with an attempt handed off before (state file) is never spoken again, in any later attempt: its new offer is acked `lost` (`handed off before`), or with its recorded `played`/`cut_off`; an attempt acked `deferred` or `dropped` before audio leaves no hand-off, so the next attempt is spoken. When the state file cannot record the hand-off after the service's 2xx, the attempt is not played and is acked `deferred` (`hand-off not recorded`). Its tts frames carry `sentence_id` and `ack:true`, and the session is held until `played`, 8 s after `tts stop`, or close. Each attempt gets exactly one terminal ack (`played`, `cut_off`, `lost`, `dropped` or `deferred`); playback evidence after it is ignored (`agent played for sentence … after speech … ended`). agent-service is the only retry owner: a proactive `failed` before audio, or a queue refusal, is acked `deferred` (`retry_after_ms`) and the gateway does not resubmit; the service offers the delivery again with the next `attempt` and a new lease, and the third attempt that fails is acked `dropped`. `expired` is `dropped`.
- **INTERRUPT.** An `interrupt` speech item does not wait for a quiet session: once the session is 3 s old and the user is not speaking, and no urgent or conversation delivery holds it, the gateway cuts the agent sentence playing on it (`cut_off`, reason `superseded`), stops the playback and voice turn through upstream `handleAbortMessage` (wrapped: the agent reply stream closes, wake cues are fenced; the device gets `tts stop`), then claims the item (`proactive <id> interrupts session …`). A WHEN_IDLE announcement it cut finishes first; the INTERRUPT item plays as its own sentence.
- **Chat switch.** `switch_chat` posts `POST /v1/chats/{id}/switch`, `new_chat` posts `POST /v1/chats` (created and current); the receipt waits for the service (at most 3 s): `accepted` on 2xx, else `stale` (the device shows `Couldn't switch`); a repeat while the post runs gets no receipt. Then the refreshed list goes to the device. The provider's next turn posts to `chats/current` after waiting up to 1 s for a switch still in flight.
- **Responses.** `approve`/`deny`/`answer` whose `card_id` is not the mirrored task's current card are dropped (`… stale … dropped`, receipt `stale`); a task the gateway does not mirror is forwarded. One decision per card: the first `approve`/`deny`/`answer` for a `card_id` (per device, last 1024 cards) that is recorded wins, and any later one, a device retry with a new `seq` included, is dropped (`… duplicate decision … dropped`, receipt `duplicate`). In live mode the receipt waits for agent-service: the post is retried after a failed answer (0.5, 1, 2, 4 s; a request may run the rest of the window and is never sent again while it runs) for 12 s with one decision id per card, kind and answer (`uuid5(device:card:<card_id>:<kind>:<option or text hash>)`, also the `Idempotency-Key`), so a later tap of the same decision after a lost answer gets the service's stored answer; `accepted` on 2xx, `stale` on 4xx or no answer (`agent <kind> … service=<accepted|rejected|unconfirmed> result=…`). While a card's decision is being forwarded, other responses for that card get no receipt (the device sends them again). A repeat of a (`boot`, `seq`) already seen is not processed again and gets that event's receipt (`agent receipt … result=…`).
- **Closing window.** A session the device opened for a proactive invocation that no longer has a lease (`proactive session opened with no current lease for it; closing once the device is quiet`) hears no voice and stays open until no agent notification arrived for 2 s (at most 8 s), so responses and `shown` acks the device sends in it are handled and receipted; then it closes. The rest are logged as `agent response …`; in live mode posted to agent-service: `approve`/`deny`/`answer` to the chat's routes (chat id URL-quoted, the card decision id above), `cancel` to `interrupt{reason:"panel"}` (`Idempotency-Key = uuid5(device:boot:seq)`), `switch_chat`/`new_chat`/`ack_seen` to the chat routes.
- **Acks.** Logged (`agent ack <outcome> …`), kept at `GET /weixue/agent/deliveries`, and in live mode posted to `POST /v1/deliveries/{id}/ack` with the offer's `lease_id` (played and cut acks also carry `sentence_id`, `boot`, `device_seq`, `cut_at_ms`, `reason`): one at a time per delivery in the order they arose, each retried (1 s doubling to 30 s) until a 2xx or 4xx answer, for at most 10 min.
- **State file.** `$WEIXUE_GATEWAY_STATE_DIR` (default `data/weixue-gateway/`, 0600 JSON written atomically and fsynced with its directory; the previous generation is kept as `<name>.prev` and read when the current file is missing or does not parse): `agent.json` keeps the last 256 card decisions, spoken attempts handed off (`delivery_id`, `attempt`, state), wire seqs per delivery and the acks not yet answered (posted again after a restart); `proactive.json` keeps every proactive message with an idempotency key for 24 h, so a key seen before a restart is a duplicate and never queued again (a message queued or delivering at the restart reads `failed` / `partial`, `gateway restarted …`).

## Service deliveries (agent-service outbox)

Offers on `GET /v1/deliveries/stream` have kind `agent` (task cards and notices) or `chats`; acks go to `POST /v1/deliveries/{id}/ack`.

- **Activity.** A task's working deliveries carry `progress: {phase, step, tool, label, activity}`; the gateway passes `step`/`total` and `activity` (cut to 24 chars) to the wire item. agent-service sets `activity` from the current tool (`docs/agent-service.md`, Turn loop), changes it at most once a second per task and clears it when the turn ends.
- **Notices.** `notify_user` is an `agent` delivery of its own card: `task_id` = `ntf_<ulid>` (no task row), `state: unread`, `title` `Notice` / `通知`, `summary` its text (≤ 60), `speak` its text, tier `INTERRUPT` (`urgent`) or `WHEN_IDLE`, `expires_at` 1 h; the payload adds `notice: true` and `origin_task_id`. The gateway carries it as any task item (mirror, speech through the proactive queue with `urgent` for INTERRUPT, playback and `ack_seen` acks); a tap on it clears it.
- **Lease and attempt.** Every ack carries the offer's `lease_id` and may carry its `attempt`; one that is not the current offer's is `409 stale_lease`, and the gateway forgets that offer. A revoked stream's offers return to pending without a lease.
- **Retry owner.** The service: `deferred` (only before any audio) returns the delivery to pending with `attempt + 1`; the next offer has a new lease and `Idempotency-Key` `<delivery_id>:<attempt>`. The gateway does not resubmit an offer it acked `deferred`.
- **Monotonic acks.** `accepted` only from `offered` (a repeat is idempotent; otherwise `409`, and the speech is not queued); `spoken` from `offered|handed_off`; `played` and `cut_off` from `offered|handed_off|uncertain` (a late `played` upgrades an uncertain card); `lost` from `offered|handed_off`; `dropped` from `pending|offered|handed_off`; `shown` from `pending|offered`. Any other ack is recorded and changes nothing (`200`, `detail: ignored in <state>`); `deferred` after playback evidence is `409`.
- **Restart.** Speech that was offered or handed off without `played|cut_off` is never offered again: its card goes unread with `playback: uncertain`. Unspoken offers return to pending.
- **Snapshot.** `GET /v1/devices/{id}/snapshot` is `{cards: [the latest delivery per slot of the last 24 h in offer shape, speak null], chats: <GET /v1/chats body>}`; the gateway restores it before the device's first delivery stream (Gateway, Restore).
- **Decisions.** `approve`/`deny`/`answer` answer 2xx once the decision is recorded and the stored answer again for a repeated `decision_id`; the gateway receipts the device only after that answer (Gateway, Responses).

## Device tools (ADR-13)

The agent backend never operates the device's physical inputs (touch, BOOT button, gestures) and never answers an approval or clarify card itself: card responses come only from a person at the device. Device MCP tools have two classes:

- **agent-visible**: `self.get_device_status`, `self.audio_speaker.set_volume`, `self.screen.set_brightness`, `self.screen.set_theme`, `self.screen.set_bedside`, `self.imu.get_state`. Only these may reach the model, through the xiaozhi device-MCP client or the gateway's `device_action`.
- **operator-only**: every other name (`self.system.reconfigure_wifi`, the upstream user-only tools such as `self.reboot` and `self.upgrade_firmware`, camera, lamp, any later tool). Never listed or callable on the MCP channel; input injection exists only on the diag build's USB serial console.

The device enforces the classes (`docs/firmware.md`, Device MCP tools). The server is the second line: `server/overlay/weixue_device_tools.py` drops a non-agent-visible tool from the device's `tools/list` before the model's function list is built (`device tool '<name>' dropped: not agent-visible`) and refuses a `call_mcp_tool` for one without sending it (`device tool call '<name>' refused: not agent-visible`). A gateway `device_action {tool, args}` runs through that same client call and checks `weixue_device_tools.is_agent_visible(tool)`.

## Fixture

```bash
WEIXUE_PROACTIVE_TOKEN=... server/scripts/agent_msg_fixture.py --base http://<host>:8003 --device <mac> <scenario>
```

Scenarios: `states`, `approval`, `clarify`, `unread`, `burst`, `seed` + `drop`, `busy`, `spoken`, `ui` (the device UI tour across three chats), `chats` (a raw `agent_chats` of five chats with the `ui` prefix's chat ids), `eight` (8 full-size tasks: paged poll bodies, the snapshot after a board reset), `closing` (an announcement plus an approval, answered on the device while the server closes the session), `view`. `agent_sim_device.py --weak-link 1440` cuts poll responses after one TCP segment of the device, `--old-firmware` treats such a body as pending as v1 firmware did, `--invoke-at S` opens a session without a current lease. On the board, the cards of `approval`, `clarify` and `closing` are answered on a diag build (`WEIXUE_FACE_DIAG=1`) over USB with `diag card approve|deny|option <n>`, `unread` with a `diag tap` on its complication; `diag volume 0` and `diag bedside day` keep that boot silent and off the bedside page (docs/firmware.md, Diag injection). `server/scripts/agent_sim_device.py` plays the device side without hardware (`--level 1` as v1.1 firmware; `--tap approve|deny` answers cards and unread items 2 s after they apply, as a finger). `server/scripts/agent_chats_e2e.py` runs the chat-switching acceptance on the sim stack (`server/sim/docker-compose.agent.yml`): `new_chat` until five chats exist, ten `switch_chat`s each followed by a voice turn that must land in the selected chat (provider `turn_latency chat=` and an `accepted` event with the turn's trace id in that chat's events), and with `--restart <container>` the chat index, current chat and device list unchanged after an agent-service restart.
