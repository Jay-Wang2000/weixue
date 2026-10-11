# Proactive announcements (feat-proactive)

A Home Assistant automation calls a webhook on the backend; the device speaks the message (`announce`) or speaks it and listens for a short answer (`conversation`).

```
HA automation --POST /weixue/proactive/messages--> xiaozhi-esp32-server (8003)
                                                     | queue (TTL, dedup, mute)
          device session open and quiet  <-----------+-----------> device idle: long-poll
          (spoken inside the session)                              GET /weixue/proactive/poll -> 200
                                                                   device opens a session with
                                                                   listen detect "weixue:proactive"
```

| Part | File |
|---|---|
| Queue, delivery rules, HTTP routes | `server/overlay/weixue_proactive.py` |
| Hooks into upstream (session registry, `listen detect` intercept, routes on port 8003) | `server/overlay/weixue_patches.py` |
| Device long-poll and session open | `firmware/patches/10-proactive-poll.patch` |
| Tests | `server/tests/test_proactive.py`, `server/tests/test_weixue_patches.py` |

The device keeps no WebSocket open while idle, so the server cannot push to it. The firmware patch adds a task that, only while the device is idle, holds `GET http://<OTA host>/weixue/proactive/poll` (header `Device-Id`). The server answers `204` after 25 s with nothing to deliver and `200` with `{"pending": true, "speak": true, "urgent": <bool>, "id": "<message id>", "lease": "<nonce>"}` as soon as a message is deliverable; that message is leased to the device and `urgent` is its own flag. A poll with `X-Weixue-Agent-Seq` also gets `200` when only agent items are pending (`speak: false`, `id` and `lease` null) and an `agent` array in either case (`docs/agent-protocol.md`). On `200` with `speak: true` the device opens a session as for a wake word, sending `listen detect` with the text `weixue:proactive` instead of "Jarvis"; the server answers it with the leased message instead of the LLM. Failed polls back off from 5 s to 60 s; a `200` whose body did not arrive whole (shorter than its Content-Length, or not JSON) is polled again after 1 s and opens no session.

The invocation text may name its lease: `weixue:proactive:<id>:<lease>` delivers only that message under that lease. The current firmware sends the bare `weixue:proactive`, which delivers the device's lease only when it was issued before the session opened. Either way an expired or replaced lease, or a lease for another message, closes the session and consumes nothing (Delivery rules). For a verified device the session is closed once it was quiet: it hears no voice, and agent notifications the device still sends in it are handled until none arrived for 2 s (at most 8 s; `docs/agent-protocol.md`, Closing window).

## Device identity

Device requests (the poll, the WebSocket session and its `weixue:proactive` invocation) are accepted only for an identity verified under the server's `server.auth` (the same check as the WebSocket server, `weixue_patches.unlisted`): auth enabled and a `Device-Id` on `allowed_devices` (exact match); only with `allowed_devices` empty, a `Bearer` token issued by the OTA endpoint for the request's `Client-Id` and `Device-Id`. With `server.auth.enabled: false` the poll answers `503` and no session can take a message. An unverified poll gets `401` and leaves no state; an unverified session is never registered and its invocation is closed without consuming anything. Under the allow-list a listed `Device-Id` header is the whole credential, so any client on the LAN that sends it is accepted as that device; a per-device credential sent with the poll (option D) is future work. Server auth settings: `docs/server.md`.

Session liveness follows the connection: a session is registered while its handler runs. A poll from a device that still has registered sessions pings them; one that does not answer within 3 s is unregistered and closed, so a second client polling with the device's id cannot hide a live session. At most 8 polls are held at once (`429` beyond that, one per device: a newer poll from the device ends its older one with `204`), and at most 32 devices are remembered.

## Setup

1. `server/.env`: `WEIXUE_PROACTIVE_TOKEN=<openssl rand -hex 32>` (chmod 600). Without it every authenticated route answers `503`. The device side needs `server.auth` enabled with the device on `allowed_devices` (Device identity).
2. `server/scripts/setup.sh` (compose passes the variable to the server container).
3. Firmware built with `firmware/build.sh` (applies `firmware/patches/*.patch`) and flashed with `firmware/flash.sh`. The boot log shows `Application: Proactive poller: http://<host>:8003/weixue/proactive/poll`.

## API

All routes except `/poll` need `Authorization: Bearer <WEIXUE_PROACTIVE_TOKEN>`; `/poll` needs a verified device identity. Port 8003 is plain HTTP on the LAN.

| Route | Purpose |
|---|---|
| `POST /weixue/proactive/messages` | Queue a message. `202` new, `200` duplicate (`"duplicate": true`), `400` invalid, `401`, `409` key reused with a different body, `429` rate limited or queue full (`Retry-After`), `503` token unset |
| `GET /weixue/proactive/messages/{id}` | Status of one message |
| `GET /weixue/proactive` | Devices (session open, last poll, poll held, seconds since the last poll ended, reachable), mute state, queued messages; also accepts `MONITOR_TOKEN` (read scope, no other route) |
| `PUT /weixue/proactive/mute` | `{"minutes": 0-1440}`; `0` unmutes |
| `GET /weixue/proactive/poll` | Device long-poll (Device identity): `200` with `speak`, the leased message's `id`, `urgent` and `lease`, and agent items (`docs/agent-protocol.md`), `204`, `401` unverified, `429` too many polls, `503` `server.auth` disabled |

Request body:

| Field | Default | Rule |
|---|---|---|
| `message` | required | 1-500 characters, spoken as written (Chinese text gets the Mandarin voice) |
| `mode` | `announce` | `announce` or `conversation` |
| `context` | `""` | Up to 1000 characters for the LLM (what the event is, what to do on yes/no); not spoken |
| `ttl` | 600 | 10-3600 s; undelivered messages then become `expired` |
| `urgent` | `false` | Delivered while muted; lights the device's bedside page fully (`docs/firmware.md`, Bedside page) |
| `device` | any | Device MAC; unset means the first device that can take it |
| `id` | none | Idempotency key; the `Idempotency-Key` header takes precedence |

Response and status record: `id`, `mode`, `status`, `reason` (while queued), `attempts`, `created_at`, `finished_at`, `reply` (conversation), `error`.

| Status | Meaning |
|---|---|
| `queued` | Waiting; `reason` is `offline`, `busy`, `muted`, or empty when the device is about to take it |
| `delivering` | Being spoken, or a conversation waiting for its answer |
| `delivered` | Announcement's audio sent to the end, or interrupted by the user after its audio started |
| `answered` | Conversation answered; `reply` is the transcript |
| `no_reply` | Conversation spoken, nothing said within the reply window |
| `partial` | After some of the audio was written: session lost, a write failed, playback timed out, synthesis failed for part of the text, or an error (`error after audio started`); not retried |
| `expired` | TTL passed before delivery |
| `failed` | 3 attempts failed before any audio was written (`error`: `no_audio` when synthesis produced none, `send_failed` when a write to the device failed, `closed`, `superseded`, `no_tts`, `timeout`, `error`) |

## Delivery rules

- **Idle device**: the held poll leases the first deliverable message to the device and returns `200` with its `id`, `urgent` and `lease`; the device opens a session and that message plays within the poll round trip plus session setup. A lease holds the message for 15 s (its TTL does not expire it meanwhile, other devices and in-session delivery skip it); a device holds one lease at a time, so a newer poll's lease replaces the older one. An invocation delivers only a current lease of its device: the one it names, or for the bare `weixue:proactive` the one issued before its session opened. It closes the session, consuming nothing, when there is none (an expired or replaced lease, a lease issued after the session opened, a wrong id or nonce), or when the leased message is not urgent and mute started since.
- **Busy device** (a session is open): the message waits until the session is quiet (not speaking, no user voice and no audio for 1.5 s, session older than 3 s), then plays inside that session. That session is not closed afterwards.
- **Offline device** (no session and no poll in the last 60 s): queued until the device polls or opens a session, or until the TTL.
- **Muted**: everything except `urgent` waits until the mute ends or the TTL passes.
- **One delivery**: a message is claimed (status `delivering`, attempt counted) before its delivery task starts; only that claim finishes or requeues it.
- **Outcome**: taken from the message's own sentence (`docs/server.md`, patch list): the audio packets the transport actually wrote for it, whether its LAST completed with every packet written while it still owned the session, and whether any part failed (a synthesis request raised, a text segment got no audio, a write raised or was cancelled). Heard (completed, or interrupted after audio was written) finishes it; audio cut off by a lost session, a failed write or the 90 s playback limit, or a part that failed after audio was written, finishes it as `partial` (`error` names which).
- **Retry**: a failure or an error before any audio was written (synthesis produced no audio, a write failed, session closed, a newer turn took over, TTS not ready, timeout, an exception) puts the message back in the queue (3 attempts, within the TTL); the second attempt waits 2 s, the third 5 s.
- **Order**: first in, first out, one at a time per session.
- **Dedup**: a repeated idempotency key returns the original record (keys kept 24 h); without a key, an identical request within 60 s is a duplicate.
- **Rate limit**: 5 new messages in a burst, then one per 10 s per client address; duplicates are not counted. At most 20 messages queued.
- An announcement in a session the device opened for it closes the session when playback ends.
- The queue is in memory; a server restart drops queued messages and dedup keys.

## Short-reply window (`conversation`)

The message is added to the session's dialogue as the assistant's turn, after a note carrying `context` and the reply language. After playback the device listens (realtime mode); the next user turn is the answer, goes to the LLM as a normal turn and is stored as `reply`. Nothing said within 8 s gives `no_reply` and, in a session opened for the message, closes it without a goodbye. Speech that starts inside the window extends it up to 20 s. The session's own no-voice timeout (`close_connection_no_voice_time`) still applies and ends the window when shorter. Upstream transcribes an utterance only when it and the 0.6 s kept before voice onset exceed 0.9 s; a 0.29 s 「嗯」 clip was recognised (see Results).

## Home Assistant example (not applied)

`secrets.yaml`:

```yaml
weixue_proactive_auth: "Bearer <WEIXUE_PROACTIVE_TOKEN>"
```

`configuration.yaml`:

```yaml
rest_command:
  weixue_proactive:
    url: "http://<LAN_IP>:8003/weixue/proactive/messages"
    method: post
    content_type: "application/json"
    headers:
      Authorization: !secret weixue_proactive_auth
      Idempotency-Key: "{{ key }}"
    payload: >-
      {"mode": {{ mode | default('announce') | tojson }},
       "message": {{ message | tojson }},
       "context": {{ context | default('') | tojson }},
       "ttl": {{ ttl | default(600) }},
       "urgent": {{ urgent | default(false) | tojson }}}
```

Automation:

```yaml
alias: Kitchen leak - ask on weixue
triggers:
  - trigger: state
    entity_id: binary_sensor.kitchen_leak
    to: "on"
actions:
  - action: rest_command.weixue_proactive
    data:
      key: "{{ trigger.to_state.context.id }}"
      mode: conversation
      urgent: true
      message: "Water detected under the kitchen sink. Do you want me to turn off the main water valve?"
      context: "binary_sensor.kitchen_leak turned on. The main valve is switch.main_water_valve."
```

`trigger.to_state.context.id` is unique per state change, so HA retries of the same run are deduplicated. The LLM acts on the answer with the tools the voice policy allows (`docs/ha-mcp.md`); the webhook itself never calls HA.

## Manual check

```bash
set -a; . server/.env; set +a
curl -s -X POST http://<LAN_IP>:8003/weixue/proactive/messages \
  -H "Authorization: Bearer $WEIXUE_PROACTIVE_TOKEN" -H "Idempotency-Key: test-$(date +%s)" \
  -H "Content-Type: application/json" \
  -d '{"mode":"conversation","message":"Do you want me to turn off the main water valve?"}'
curl -s -H "Authorization: Bearer $WEIXUE_PROACTIVE_TOKEN" http://<LAN_IP>:8003/weixue/proactive
```

Without the device, `server/scripts/proactive_sim_device.py` plays the device side (test venv, `websockets` and `opuslib_next` from `server/requirements-test.txt`; `--device` must be on `allowed_devices`; `--token` only with `allowed_devices` empty):

```bash
DYLD_FALLBACK_LIBRARY_PATH=.venv/lib .venv/bin/python server/scripts/proactive_sim_device.py \
  --base http://<LAN_IP>:8003 --ws ws://<LAN_IP>:8000/xiaozhi/v1/ --device <MAC> --reply yes.wav   # then POST a message
```

`server/sim/` runs the same flows without credentials or the live stack: a compose project `weixue-wave3-sim` (container `weixue-wave3-sim-server`, `127.0.0.1:18100/18103`) from the built server image with the working tree's overlay mounted, a local tone generator in place of the Google TTS client, scripted ASR and a one-line LLM. `server/sim/scenarios.sh` is a pass/fail gate (usage in its header): each scenario asserts HTTP statuses, terminal outcomes, attempts and server log lines with idempotency keys unique to the run, every wake cue is covered, and the script exits 1 on any failed check; the raw sim-device and server output goes to a separate transcript file. `proactive_sim_device.py --bind` sends the lease-naming invocation, `--invoke-delay` delays the session, `--abort-after` sends an abort after the detect.

Server log lines: `proactive <id> queued`, `delivering: attempt=1 session=<id> invoked=True`, then `delivered`, `answered`, `no_reply`, `expired` or `failed`. Device serial: `Proactive message pending`, `Proactive session opened`, `<< <message>`.

## Results (2026-10-06)

Logs: `docs/logs/proactive-device-2026-10-06.log` (device serial, webhook statuses, server lines), `docs/logs/proactive-sim-2026-10-06.log` (simulated device). The on-device run used this branch's firmware built with `CONFIG_OTA_URL` on port 18003, pointing the board at a separate container of the branch image on 18000/18003 with the live data and credentials, no MCP endpoint (no HA tools); the live server was not changed. Replies were played from the Mac speaker with `say`.

| Check | Result |
|---|---|
| Boot | `Proactive poller: http://192.168.8.220:18003/weixue/proactive/poll` after `Activation done`; one `Model 0: wn9_jarvis_tts` |
| Announce from idle | `202` at 22:15:39; `Proactive message pending` and session open at 22:15:40, `<< Water detected under the kitchen sink`, session closed by the server after playback, `delivered` |
| Conversation, "Yes." | Opener played 6 s; STT `Yes.` (en-US, 1.4 s) as the next turn, LLM answered in English; `answered`, `reply: "Yes."` |
| Conversation, 「嗯」 (0.29 s clip) | STT `嗯。` (cmn-Hans-CN, 1.2 s); `answered`, `reply: "嗯。"`; LLM answered in Chinese |
| Conversation, silence | Session closed 7.6 s after playback, no goodbye; `no_reply` |
| Busy device | Webhook during a "Jarvis" session: `reason: busy`; played inside that session 3 s after it opened; session stayed open; `delivered` |
| Mute | Normal message `reason: muted` and held; urgent message delivered during the mute; held message delivered 1 s after unmute |
| Simulated device | Same flows plus "No." (`answered`) against the branch image |
| Committed build (:8003) against a server without these routes | Poller gets `404` and backs off 5 s, 10 s, 20 s |
| Unit tests | `server/tests/test_proactive.py`, `test_weixue_patches.py`: auth, rate limit, dedup, TTL, mute, offline/busy reasons, long-poll, stale sessions, retry/fail, reply window, hooks |

### Live stack after rebase onto `main` (2026-10-06)

Logs: `docs/logs/proactive-integration-2026-10-06.log` (serial, webhook responses, server lines), `docs/logs/proactive-integration-flash-2026-10-06.log`. Branch rebased onto `main` with `05-board-power-usb.patch`; firmware built with `CONFIG_OTA_URL` on :8003 and flashed; the live `xiaozhi-esp32-server` and `mcp-bridge` containers recreated from the branch image with `WEIXUE_PROACTIVE_TOKEN` set and `data/.config.yaml` re-rendered. Mac `say` at volume 55.

| Check | Result |
|---|---|
| Boot | `Proactive poller: http://192.168.8.220:8003/weixue/proactive/poll` after `Activation done`; one `Model 0: wn9_jarvis_tts`; `activating -> idle` at 4.4 s |
| Wake round | `Wake word detected: Jarvis`; STT `What time is it?` (en-US, 1.6 s) -> Anthropic -> TTS "It's five to four in the afternoon" played; `Goodbye` -> `handle_exit_intent` -> idle |
| Announce from idle | `202`; `Proactive message pending` 1 s later, `<< The washing machine has finished`, session closed after playback, `delivered` (attempt 1) |
| Conversation, "yes" | `202`; opener played 4.9 s; STT `Yes.` (en-US, 1.3 s) as the next turn, LLM answered "Okay, I'll remind you in ten minutes"; `answered`, `reply: "Yes."` |
| Unit tests | 233 passed, 10 skipped (live) |

Not run: offline delivery after a device reboot, a server restart with queued messages (dropped by design), HA calling the webhook.

### Simulated device, device identity and delivery outcomes (2026-10-07)

Log: `docs/logs/fix-wave3-server-sim-2026-10-07.log` (`server/sim`, office mode: no device, no audio, live stack untouched).

| Check | Result |
|---|---|
| Poll with an unlisted or missing `Device-Id` | `401` |
| WebSocket from an unlisted device | `认证失败`, closed |
| Announce from idle | poll `200` with `id` and `urgent`, `delivered` (attempt 1) |
| Invocation with nothing reserved for the device | session closed, message stays queued |
| Synthesis fails (`InvalidArgument`) | `requeued: no_audio; retry in 2s`, then `retry in 5s`, then `failed: no_audio` (attempts 2 s and 6 s apart) |
| Synthesis fails after its first audio chunk | `partial`, `error: synthesis failed after audio started`, attempt 1 |
| Urgent conversation, "Yes." after the opener | `answered`, `reply: "Yes."` |
| Opener echo during playback, then "Yes." | `playback echo ignored`, opener played to the end, `answered` |
| "Stop." during playback | barge-in (`Abort message received`), `answered` |
| Second client polls with the device id during its open session | ping answered, session kept, message played in-session, second poll `204` |
| Wake, cue ("Mm-hm?", "Hmm?", "Yes?"), then "Yes." | each cue: `stt Yes`, LLM reply played, no `playback echo ignored` |

Not run: the device itself, a real Google TTS failure, HA calling the webhook.

### Leases, delivery outcomes, echo guard and wake cue fence (2026-10-07)

Log: `docs/logs/fix-wave5-server-sim-2026-10-07.log` (`server/sim` gate, 46 checks, all passed; office mode: no device, no audio, live stack untouched).

| Check | Result |
|---|---|
| Poll answer | `200` with `id`, `urgent` and a 32-hex `lease` |
| Lease expired before the session opened (session 16 s after the poll) | session closed (`no current lease`), message `queued`, `attempts` 0; the next poll's lease-naming invocation delivers it on attempt 1 |
| Echo of the sentence that is playing | `playback echo ignored`, no abort, `answered` with the later "Yes." |
| "Do not turn on the living room light." / "Turn off the living room light." while "I will turn on the living room light now" plays | not taken for echo, barge-in (`Abort message received`), the correction is the `reply` |
| Abort 0.2 s after the wake detect | `wake cue skipped`, no cue caption |
| Each cue ("Hmm?", "Mm-hm?", "Yes?") followed by "Yes." | `stt Yes`, LLM reply played, for all three |
| Synthesis fails before audio / after the first chunk | `failed` after 3 attempts (`no_audio`) / `partial` on attempt 1 |

Not run: the device itself (firmware still sends the bare invocation), a real transport failure on the live stack.
