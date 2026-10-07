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

The device keeps no WebSocket open while idle, so the server cannot push to it. The firmware patch adds a task that, only while the device is idle, holds `GET http://<OTA host>/weixue/proactive/poll` (header `Device-Id`). The server answers `204` after 25 s with nothing to deliver and `200` with `{"pending": true, "urgent": <bool>}` (the `urgent` field of the next message) as soon as a message is deliverable. On `200` the device opens a session as for a wake word, sending `listen detect` with the text `weixue:proactive` instead of "Jarvis"; the server answers it with the queued message instead of the LLM. Failed polls back off from 5 s to 60 s.

## Setup

1. `server/.env`: `WEIXUE_PROACTIVE_TOKEN=<openssl rand -hex 32>` (chmod 600). Without it every authenticated route answers `503`.
2. `server/scripts/setup.sh` (compose passes the variable to the server container).
3. Firmware built with `firmware/build.sh` (applies `firmware/patches/*.patch`) and flashed with `firmware/flash.sh`. The boot log shows `Application: Proactive poller: http://<host>:8003/weixue/proactive/poll`.

## API

All routes except `/poll` need `Authorization: Bearer <WEIXUE_PROACTIVE_TOKEN>`. Port 8003 is plain HTTP on the LAN.

| Route | Purpose |
|---|---|
| `POST /weixue/proactive/messages` | Queue a message. `202` new, `200` duplicate (`"duplicate": true`), `400` invalid, `401`, `409` key reused with a different body, `429` rate limited or queue full (`Retry-After`), `503` token unset |
| `GET /weixue/proactive/messages/{id}` | Status of one message |
| `GET /weixue/proactive` | Devices (session open, last poll, reachable), mute state, queued messages |
| `PUT /weixue/proactive/mute` | `{"minutes": 0-1440}`; `0` unmutes |
| `GET /weixue/proactive/poll` | Device long-poll (`Device-Id` header) |

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
| `delivered` | Announcement played to the end, or interrupted by the user |
| `answered` | Conversation answered; `reply` is the transcript |
| `no_reply` | Conversation spoken, nothing said within the reply window |
| `expired` | TTL passed before delivery |
| `failed` | Session lost during playback 3 times (`error`) |

## Delivery rules

- **Idle device**: the held poll returns `200`; the device opens a session and the message plays within the poll round trip plus session setup.
- **Busy device** (a session is open): the message waits until the session is quiet (not speaking, no user voice and no audio for 1.5 s, session older than 3 s), then plays inside that session. That session is not closed afterwards.
- **Offline device** (no session and no poll in the last 60 s): queued until the device polls or opens a session, or until the TTL.
- **Muted**: everything except `urgent` waits until the mute ends or the TTL passes.
- **Retry**: if the session closes before playback ends, the message goes back to the queue (3 attempts, within the TTL).
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

Without the device, `server/scripts/proactive_sim_device.py` plays the device side (test venv, `websockets` and `opuslib_next` from `server/requirements-test.txt`):

```bash
DYLD_FALLBACK_LIBRARY_PATH=.venv/lib .venv/bin/python server/scripts/proactive_sim_device.py \
  --base http://<LAN_IP>:8003 --ws ws://<LAN_IP>:8000/xiaozhi/v1/ --reply yes.wav   # then POST a message
```

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
