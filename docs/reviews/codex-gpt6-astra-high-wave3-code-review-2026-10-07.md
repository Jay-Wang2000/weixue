## 1. Summary verdict

**Fix-first.** Wave 3 adds useful functionality, but notification delivery, wake handling, microphone mute, and HA failure handling still have correctness gaps.

Top five issues:

1. **P1 — Proactive delivery is not authenticated end-to-end:** an unauthenticated client can impersonate the device, consume notifications, and invalidate its registry entry.
2. **P1 — Face-down mute can leave a timed-out WebSocket connected:** subsequent server messages can restart voice processing while the mute flag remains set.
3. **P1 — The recorded final timing build fails short one-breath requests:** none of eight “Jarvis, what time is it?” trials preserved the request.
4. **P1 — HA calls can outlive the caller’s timeout:** the bridge permits 120-second HTTP waits behind a 30-second executor and does not preserve an explicit unknown action outcome.
5. **P2 — A delayed wake cue can overwrite a newer conversation turn**, causing the user’s response audio to be discarded as stale.

Reviewed committed `main` at `e0c748a`, against the Wave 2 review baseline `430014e`. There is no `06` patch on this revision; screen-off changes are incorporated into `05-board-power-usb.patch`.

No files, Docker services, or device state were changed. All 34 scoped Python files passed AST parsing. Read-only, in-memory reproductions confirmed the queue-claim race, poll-induced registry invalidation, cue takeover, real “Yes” suppression, silent TTS failure, and false-positive bridge health. Full pytest/build/hardware tests were not rerun.

## 2. Correctness bugs

### C1 — P1 · feat-imu: microphone mute does not reliably close the transport

**Locations:** [12-application-mic-mute.patch:104](/Users/jwang14/Projects/weixue/firmware/patches/12-application-mic-mute.patch:104), upstream [websocket_protocol.cc:70](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/protocols/websocket_protocol.cc:70), [application.cc:624](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/application.cc:624), [application.cc:1071](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/application.cc:1071).

`SetMicMuted()` closes the channel only when `IsAudioChannelOpened()` returns true. That predicate also checks the 120-second receive timeout; false does **not** mean the underlying WebSocket has been disconnected.

**Failure scenario:** A slow server turn exceeds the device timeout. Face-down mute disables processing and returns the device to idle, but skips transport closure. A subsequent `tts start`—for example, a proactive delivery on the surviving server session—can enter Speaking. Its `tts stop` enters Listening, whose `StartListeningAudio()` unconditionally enables voice processing. The microphone can stream while `mic_muted_` and the mute glyph still say muted.

**Fix:** Close any existing transport unconditionally on mute, invalidate queued session callbacks, and enforce the mute invariant in incoming TTS handling, listening activation, and microphone transmission. Test mute after channel timeout and against delayed frames.

### C2 — P1 · fix-conversation-timing: short requests spoken with the wake word remain broken

**Locations:** [weixue_patches.py:204](/Users/jwang14/Projects/weixue/server/overlay/weixue_patches.py:204), upstream [application.cc:958](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/application.cc:958), recorded [one-breath results:9](/Users/jwang14/Projects/weixue/docs/logs/conversation-timing-onebreath-2026-10-07.log:9).

The final build’s eight short-request trials produced six bare “Jarvis” transcripts, “Jarvis, who is it?”, and “Jarvis, answer”. Longer requests also lost their opening words. The documented capture gap is 286–330 ms, followed by poor initial live-audio transcription.

Preserving pre-roll and withholding VAD stop cannot recover audio that the firmware never captured. This is an observed shipped-path defect; the evidence does not establish that Wave 3 originally introduced the capture gap.

**Fix:** Preserve continuous microphone capture through wake detection, connection setup, and transition into listening. Retest short English and Chinese commands spoken both continuously and after the cue.

### C3 — P1 · feat-ha-mcp: tool timeout and action outcome are inconsistent across the bridge

**Locations:** [hamcp.py:8](/Users/jwang14/Projects/weixue/server/mcp-bridge/weixue_bridge/hamcp.py:8), [bridge.py:129](/Users/jwang14/Projects/weixue/server/mcp-bridge/weixue_bridge/bridge.py:129), upstream [mcp_endpoint_handler.py:287](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/providers/tools/mcp_endpoint/mcp_endpoint_handler.py:287).

The HTTP client permits a 120-second read wait, while the server’s endpoint caller abandons the result after 30 seconds. The bridge continues independently. HTTP read/write failures are reduced to `HaMcpError`, then reported as “Home Assistant is unreachable right now.”

**Failure scenario:** HA receives a `light.toggle`, but its response stalls or is lost. The user receives a timeout/unreachable response although the action may already have executed—or may execute after the voice caller has abandoned it. Repeating the command can reverse the successful action.

**Fix:** Propagate a shared deadline below the caller’s remaining budget and distinguish failure before dispatch from **dispatched, outcome unknown**. Preserve correlation for late outcomes; never imply non-execution or retry an ambiguous action automatically. Shortening the HTTP timeout alone is insufficient.

### C4 — P2 · fix-conversation-timing: cue loading can overwrite a newer turn

**Location:** [weixue_patches.py:185](/Users/jwang14/Projects/weixue/server/overlay/weixue_patches.py:185).

The player checks user activity, then awaits `audio_to_data()`. On a cache miss, upstream [util.py:318](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/utils/util.py:318) performs decoding in an executor. After that await, the cue unconditionally replaces `sentence_id` and clears `client_abort`.

**Confirmed reproduction:** While clip loading was suspended, a new user turn acquired the connection. Releasing the loader overwrote that turn’s ID and emitted the cue. The actual audio sender filters by sentence ID, so the newer reply can then be discarded.

**Fix:** Capture a generation before scheduling the cue; revalidate generation, voice activity, abort and connection state after every await. Cancel pending cue tasks on turn start and close.

### C5 — P2 · fix-conversation-timing: echo suppression drops genuine short answers

**Location:** [weixue_patches.py:100](/Users/jwang14/Projects/weixue/server/overlay/weixue_patches.py:100).

Any transcript reduced to a cue word—including `yes`—within five seconds of cue start is discarded. This neither verifies an acoustic echo nor limits suppression to the particular clip played.

**Confirmed reproduction:** A genuine `"Yes."` immediately after an `"Hmm?"` cue is classified as echo. Proactive delivery may begin inside that interval in an existing session, so this also conflicts with short-answer support.

**Fix:** Use playback/AEC evidence and a narrowly bounded echo interval; preserve genuine affirmative answers. Add a real-user “Yes” case, rather than only tests asserting that cue words are consumed.

### C6 — P2 · feat-proactive: messages are claimed after scheduling, allowing duplicate delivery

**Locations:** [weixue_proactive.py:401](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:401), [weixue_proactive.py:420](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:420), [weixue_proactive.py:443](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:443).

`next_for()` selects a queued message, but its status changes to `delivering` only when the scheduled delivery task starts. The busy flag belongs to the connection, not the message.

**Confirmed reproduction:** Two overlapping sessions using the same device ID invoked delivery before either task ran. Both delivered the same message; its attempt count became two. This can occur with overlapping/reconnecting sessions without adding a second physical device.

**Fix:** Claim the message synchronously before creating the task, record the owning session/generation, and permit only that owner to finish or requeue it.

### C7 — P2 · feat-proactive: failed synthesis can be reported as successful delivery

**Locations:** [weixue_proactive.py:530](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:530), [weixue_proactive.py:462](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:462), [google_tts.py:252](/Users/jwang14/Projects/weixue/server/overlay/core/providers/tts/google_tts.py:252).

`speak()` treats clearing the shared speaking flag as proof that the announcement was spoken. Google TTS can catch a synthesis error and enqueue an empty terminal marker; normal terminal processing clears that flag.

**Confirmed reproduction:** The actual TTS method, given a synthetic `InvalidArgument`, returned success and queued FIRST/LAST with zero audio. The proactive path has no synthesis-result check to distinguish this from playback completion.

**Failure:** A leak announcement disappears from the queue as `delivered`, although no speech was generated.

**Fix:** Await a sentence-specific completion result carrying synthesis success, audio sent, interruption and failure. Retry only failures known to precede delivery; distinguish partial/unknown delivery.

### C8 — P2 · feat-ui-face: slow work is shown as speaking after 20 seconds

**Location:** [face_display.cc:274](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:274).

Thinking expires after a fixed 20 seconds. Upstream enters device Speaking on STT receipt, before any answer audio. Consequently a valid 25-second tool/LLM wait changes from thinking dots to talking eyes while remaining silent.

**Fix:** End Thinking on a turn-specific first-speech or terminal event. A timeout should show an explicit stalled/error state, not fabricate speech activity.

### C9 — P2 · feat-imu: timing assumptions fail after polling overruns

**Locations:** [20-board-imu-gestures.patch:101](/Users/jwang14/Projects/weixue/firmware/patches/20-board-imu-gestures.patch:101), [qmi8658.cc:50](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/qmi8658.cc:50), [imu_gestures.h:55](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/imu_gestures.h:55).

The detector assumes every update represents 20 ms, but individual I²C operations may block for 50 ms. After an overrun, `vTaskDelayUntil()` advances the old schedule and can run successive iterations without delay. The driver does not verify a fresh sample, and read failures leave detector history/timing intact.

**Failure scenario:** Bus contention or a transient read fault is followed by rapid catch-up reads. Repeated/current samples are credited as independent 20-ms observations, weakening impulse rejection and the sustained-motion/orientation thresholds. Hardware occurrence was not demonstrated.

**Fix:** Resynchronize after missed deadlines, process fresh samples only, and reset sustained-condition history across invalid sampling gaps. Test stalled I²C and recovery, not only perfectly spaced synthetic samples.

## 3. Security and secrets

### S1 — P1 · feat-proactive: webhook authentication does not protect notification consumption

**Locations:** [weixue_proactive.py:597](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:597), [weixue_proactive.py:611](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:611), [weixue_patches.py:50](/Users/jwang14/Projects/weixue/server/overlay/weixue_patches.py:50), [config.template.yaml:5](/Users/jwang14/Projects/weixue/server/config/config.template.yaml:5).

Bearer verification protects creation/status/mute routes, but `/poll` accepts an arbitrary `Device-Id`, and the unauthenticated WebSocket accepts `weixue:proactive`.

**Failure:** A reachable LAN client impersonates the board and receives/removes its queued household notifications without knowing the webhook token. Polling with that ID also updates `poll_started`; `live_sessions()` then excludes the real, still-open session.

The registry invalidation was reproduced directly: one live session became zero after another poll. Arbitrary IDs also accumulate in `last_poll`/`poll_started`, which have no eviction.

**Fix:** Authenticate both polling and WebSocket invocation, bind them to the same device identity, and derive session liveness from actual connection lifecycle. Bound registry entries and polling concurrency. A MAC header alone is not authentication.

**Wave 2’s unauthenticated HA entry remains open.** Ports 8000/8003 still bind all host interfaces, and authentication remains disabled. Now those sessions receive actual HA tools. [compose:18](/Users/jwang14/Projects/weixue/server/docker-compose.yml:18), [voice_bridge.json:2](/Users/jwang14/Projects/weixue/server/policy/voice_bridge.json:2). Resolve the approved network boundary before day-to-day use; [docs/server.md:18](/Users/jwang14/Projects/weixue/docs/server.md:18) still incorrectly promises firmware-base will enable it.

**Secret handling:** Constant-time webhook-token comparison and the bridge’s URL redaction/type-only transport errors are good. The tracked-text scan found no high-confidence API-key or secret-path values. However, the webhook bearer travels over plain HTTP, upstream logging still records credential-bearing endpoint URLs, and the new [serial_log.py:38](/Users/jwang14/Projects/weixue/firmware/tools/serial_log.py:38) writes raw serial bytes without the IMU tool’s redaction. No central pre-persistence redaction or bounded Docker logging was added.

**Tool filter:** The committed filter is light-only and rejects disallowed tools and raw WebSocket commands. The broader policy remains a draft. Before enabling it, add absent-target cases: under the repository’s own evaluator model, `switch.turn_off` or `cover.open_cover` without `entity_id` matches no entity restriction. [policy:49](/Users/jwang14/Projects/weixue/server/policy/voice_policy.proposed.json:49), [test evaluator:85](/Users/jwang14/Projects/weixue/server/tests/test_voice_policy.py:85). This is a demonstrated draft-policy gap, not a verified live HA bypass.

## 4. Reliability

### R1 — P2 · feat-ha-mcp: heartbeat accepts failed RPCs as healthy

**Location:** [bridge.py:164](/Users/jwang14/Projects/weixue/server/mcp-bridge/weixue_bridge/bridge.py:164).

The heartbeat marks healthy whenever `ha.send()` does not raise. That method returns JSON-RPC errors normally and returns `None` for HTTP 202.

**Confirmed reproduction:** A matching ping response containing `error` refreshed health.

**Fix:** Require a valid successful ping result before refreshing health. Test RPC errors, HTTP errors, endpoint disconnects and recovery through `run()`, not just health-file age.

Other reliability conclusions:

- **ADR-11 remains unimplemented.** The timing patch refreshes server activity while speaking; it does not send device keepalives or enforce a 90-second absolute deadline. Firmware still expires after 120 seconds without incoming application traffic. The server’s secondary inactivity timeout is now **68 seconds**, but continuous microphone processing while `client_is_speaking` can keep refreshing it indefinitely. [weixue_patches.py:84](/Users/jwang14/Projects/weixue/server/overlay/weixue_patches.py:84), [upstream timeout:184](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/connection.py:184), [firmware timeout:109](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/protocols/protocol.cc:109).
- **Restart loses mute as well as messages and dedup keys.** All reside in `Proactive` memory. The documented queue-loss limitation is explicit; mute reset is not. Previously accepted messages vanish, retries can replay previously delivered messages, and temporary mute disappears. Define these semantics before relying on the webhook for alerts. [weixue_proactive.py:203](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:203), [docs/proactive.md:76](/Users/jwang14/Projects/weixue/docs/proactive.md:76).
- **Bridge reconnect is better than recovery assurance.** Endpoint disconnects have bounded backoff and re-announcement. Tests do not exercise that loop or HA restart recovery. A fresh heartbeat file does not verify tool routing; `unless-stopped` does not restart an unhealthy but running process.
- **Device locking remains advisory.** Flash, backup, serial capture, IMU trials and stack setup do not enforce one shared ownership protocol. The workflow additionally permits age-only lock breaking and unconditional `rm -rf`, allowing an old owner to remove a replacement owner’s lock. [workflow:8](/Users/jwang14/Projects/weixue/.claude/workflows/wave-gate.js:8), [serial_log.py:33](/Users/jwang14/Projects/weixue/firmware/tools/serial_log.py:33), [imu_gesture_test.py:59](/Users/jwang14/Projects/weixue/firmware/tools/imu_gesture_test.py:59). Use owner-checked leases spanning flash through test; this extends the still-open Wave 2 locking finding.

No additional confirmed power/screen-off logic defect was found. The VBUS guard preserves USB operation, and panel changes/wakes are serialized through the ESP timer task. Actual touch and IMU relight acceptance remains incomplete.

## 5. Test quality and hardware gaps

The strongest additions test webhook validation/dedup/rate limits, load the real upstream idle-close function, and exercise bridge failure text through the upstream executor.

Important gaps:

| Area | Assessment |
|---|---|
| Proactive delivery | Most tests replace playback with `Speaker`. The direct `speak()` test manually clears `client_is_speaking`; it cannot detect zero-audio delivery or sentence ownership failures. [test_proactive.py:541](/Users/jwang14/Projects/weixue/server/tests/test_proactive.py:541) |
| Wake cue | The recorder does not implement real sender filtering or terminal state changes. Missing cases include a new turn during clip loading and cancellation on close. [test_wake_cue.py:166](/Users/jwang14/Projects/weixue/server/tests/test_wake_cue.py:166) |
| Echo filtering | Tests bless dropping cue words without distinguishing actual user speech. [test_wake_cue.py:294](/Users/jwang14/Projects/weixue/server/tests/test_wake_cue.py:294) |
| IMU | Host tests cover idealized acceleration traces. “Double tap on table” tests rejection by the pickup detector, not the QMI tap engine. The trial tool counts detection logs, not successful relight, mute or playback stop. [imu_gestures_test.cc:115](/Users/jwang14/Projects/weixue/firmware/tests/imu_gestures_test.cc:115), [imu_gesture_test.py:137](/Users/jwang14/Projects/weixue/firmware/tools/imu_gesture_test.py:137) |
| Bridge | Good request/filter/error tests; no reconnect-loop, RPC-health-error, slow-action or lost-response tests. [test_mcp_bridge.py:514](/Users/jwang14/Projects/weixue/server/tests/test_mcp_bridge.py:514) |
| Policy | A copied evaluator model cannot establish equivalence with the deployed policy engine. Missing-target cases are absent. |

**LVGL assessment:** The release renderer uses small draw descriptors and retained objects, not a full-screen per-frame bitmap. Integer-geometry invalidation limits work; LVGL copies the submitted descriptors. No draw-event lifetime or framebuffer allocation defect was found. [face_display.cc:561](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:561).

Recorded idle frames average 9–10 ms, with a 68-ms maximum; idle neutral coverage is 13.43% of the circle. These support resource/coverage claims, **not** flicker-free animation. Diagnostic snapshots still hold the display lock for 306–431 ms and caused an encode-queue drop; diagnostic builds are unsuitable as evidence of uninterrupted release audio. [docs/firmware.md:183](/Users/jwang14/Projects/weixue/docs/firmware.md:183).

**Still untested or incomplete on hardware:**

- Pickup, face-down/up and double-tap: no hand trials; ≥9/10 for each is unproved. The idle run proves only no false positives on one stationary desk setup. [docs/firmware.md:200](/Users/jwang14/Projects/weixue/docs/firmware.md:200).
- Touch relight, IMU relight from fully off, mute during speech/connection setup/timeout, and double-tap during real playback. [docs/firmware.md:171](/Users/jwang14/Projects/weixue/docs/firmware.md:171).
- Visible flicker/tearing and physical appearance of transitions: explicitly not inspected. [docs/firmware.md:186](/Users/jwang14/Projects/weixue/docs/firmware.md:186).
- Proactive offline reboot/reconnect, HA-triggered webhook, restart recovery, and final timing+IMU interaction. [docs/proactive.md:178](/Users/jwang14/Projects/weixue/docs/proactive.md:178).
- Final committed reconciliation: the recorded reconciliation is for `695cd70`, before IMU, and lists only patches 05/10/11. [reconcile log:1](/Users/jwang14/Projects/weixue/docs/logs/reconcile-2026-10-07.log:1). An untracked `reconcile-imu-2026-10-07.log` appeared during this review; it is outside the reviewed committed evidence.

## 6. Spec drift and remaining Wave 2 findings

| Requirement | Status at reviewed `main` |
|---|---|
| ADR-6 / firmware-base | Single Jarvis model has boot evidence; ≥9/10 wake attempts and custom-message acceptance remain unproved. [DESIGN.md:40](/Users/jwang14/Projects/weixue/DESIGN.md:40) |
| ADR-7 | Eight-second silent follow-up is implemented, but one-breath capture, cue races and short-answer suppression prevent complete acceptance. |
| ADR-9 / feat-ha-mcp | Bridge/light-only filter shipped. Secret rotation, 5–10-second policy wait and token ownership/reconciliation are not complete despite `status: done`. Keeping unapproved policy changes unapplied is correct; the status/scope must reflect the deferral. [tasks.yml:57](/Users/jwang14/Projects/weixue/tasks.yml:57), [docs/ha-mcp.md:70](/Users/jwang14/Projects/weixue/docs/ha-mcp.md:70) |
| ADR-11 | Shared 90-second deadline and stale-result isolation remain pending. |
| ADR-12 | USB survival, dim/off and wake-word relight have logs. Touch, pickup relight and battery-transition acceptance are incomplete. |
| Face acceptance | Four states and idle pixel coverage have software/on-device dump evidence; physical animation inspection remains open. |
| IMU acceptance | `done` conflicts directly with the missing three ×10 gesture trials. [tasks.yml:97](/Users/jwang14/Projects/weixue/tasks.yml:97), [DESIGN.md:50](/Users/jwang14/Projects/weixue/DESIGN.md:50) |
| M2 | Background tasks, durable result delivery and approval UI are explicitly future work. The proactive queue is not an implementation of these guarantees. |

The [methodology:20](/Users/jwang14/Projects/weixue/docs/METHODOLOGY.md:20) requires the changed hardware feature to be exercised, not merely flashed alongside a successful voice smoke. The gate’s “fail only if acceptance-critical items fail” wording allows skipped critical checks to escape unless explicitly treated as incomplete.

**Wave 2 finding status:**

- **Still open:** C1 tool-index gaps; C2 stale tool results entering new turns; C3 stalled TTS cancellation; C4 byte-limit splitting; C5 stream-boundary whitespace corruption; C6 overly broad STT fallback; C7 backup identity/checksum guard; C8 non-atomic setup; C9 firmware provenance.
- **Still open:** S1 unauthenticated network exposure and O1 missing absolute deadline. HA integration increases their practical impact.
- **Closed:** C10 obsolete permission-bypassing launcher was removed.
- **Partial progress:** Bridge health and private endpoint-file creation were added, and patch builds now have evidence. They do not resolve whole-stack readiness, log retention, dependency/provenance or recovery concerns.

The three provider implementations are unchanged from the reviewed baseline; scheduling their fixes in `tasks.yml` does not close the findings. [pending fixes:137](/Users/jwang14/Projects/weixue/tasks.yml:137).

## 7. Concrete patch list, ordered by value

1. **`server/overlay/weixue_proactive.py`, `weixue_patches.py`, firmware patch 10:** authenticate device consumption and bind polling/invocation to verified session ownership.
2. **`server/config/config.template.yaml`, `server/docker-compose.yml`:** close the existing unauthenticated HA/voice entry using Jay’s approved network/authentication boundary.
3. **`firmware/patches/12-application-mic-mute.patch`:** close stale transports on mute and prevent every path from enabling microphone processing while muted.
4. **Connection overlay and providers:** implement immutable turn ownership plus ADR-11’s shared deadline; fence late tool/audio results.
5. **`server/mcp-bridge/weixue_bridge/{hamcp,bridge}.py`:** align request budgets and preserve dispatched-but-unknown HA outcomes.
6. **Firmware wake/audio application patch:** capture continuously through wake-to-listen setup and validate short one-breath requests.
7. **`server/overlay/weixue_patches.py`:** generation-fence/cancel wake cues and remove lexical suppression of genuine “Yes” responses.
8. **`server/overlay/weixue_proactive.py`:** claim messages before task creation and finish them from sentence-specific synthesis/playback outcomes.
9. **`20-board-imu-gestures.patch`, `imu_gestures.h`, `qmi8658.cc`:** resynchronize polling after overruns and reject stale/discontinuous sample histories.
10. **`face_display.cc`:** retain Thinking until actual speech or an explicit terminal/error event.
11. **`server/mcp-bridge/weixue_bridge/bridge.py`:** refresh health only after a successful RPC result and test the reconnect loop.
12. **Firmware/serial/stack entry points and workflow:** enforce owner-checked device locking across flash and test; remove age-only lock stealing.
13. **`server/policy/`, tests and acceptance docs:** resolve absent-target policy cases before activation, add the identified failure-path regressions, and replace premature completion claims with measured acceptance evidence.

Counts below include distinct newly reported runtime/security findings assigned to the seven requested tasks. They exclude inherited Wave 2 findings, draft-policy concerns and acceptance/process gaps.

| Task | P1 | P2 | Findings |
|---|---:|---:|---|
| fix-power-usb | 0 | 0 | — |
| fix-screen-off | 0 | 0 | — |
| feat-proactive | 1 | 2 | S1, C6, C7 |
| feat-ui-face | 0 | 1 | C8 |
| feat-imu | 1 | 1 | C1, C9 |
| fix-conversation-timing | 1 | 2 | C2, C4, C5 |
| feat-ha-mcp | 1 | 1 | C3, R1 |
| **Total** | **4** | **7** | **11 distinct findings** |

For the quality log’s **five-task serial-chain row**, excluding `fix-power-usb` and separately merged `feat-ha-mcp`: **3 P1 / 6 P2**.