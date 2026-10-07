**1. Summary verdict — Fix-first**

Reviewed `main` at **6937034**, against the Wave 3 baseline `e0c748a`. Wave 4 adds working ring/clock rendering and a useful panel prototype, but contains **1 P1 and 5 P2 correctness findings**. Physical touch acceptance remains incomplete.

Top five:

1. **P1:** A panel decision can block the application main task during connection setup, delaying wake and IMU mute handling.
2. **P2:** The card reports “sent” even when the underlying WebSocket send fails.
3. **P2:** Bedside time remains tied to a fixed-offset activation-time clock; DST and subsequent clock corrections are not handled correctly.
4. **P2:** The urgency advertised by polling can belong to a different notification from the one eventually delivered.
5. **Acceptance gap:** Virtual-pointer tests bypass CST9217 acquisition and board touch/wake callbacks. Rendering measurements also show roughly **2–3× face-only frame cost**, without physical animation inspection.

No files, Docker services or device state were changed. Read-only verification included four Python AST parses, two existing event-wrapper tests executed in memory, and a reproduction of the urgency mismatch using the production queue implementation. Firmware builds and hardware tests were not rerun.

**2. Correctness bugs**

**C1 — P1 · feat-ui-panel: device-event connection setup blocks the application main task**

Locations: [22-application-device-event.patch:88](/Users/jwang14/Projects/weixue/firmware/patches/22-application-device-event.patch:88), upstream [websocket_protocol.cc:168](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/protocols/websocket_protocol.cc:168), [application.cc:265](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/application.cc:265).

`SendDeviceEvent()` schedules a callback on the main task, then synchronously calls `OpenAudioChannel()`. Connection establishment includes a **10-second server-hello wait**, in addition to transport connection/handshake time.

**Scenario:** Tap Approve while the backend accepts a connection but does not complete the protocol handshake. The display task can continue showing “sending”, but the application cannot process wake events, audio-send events or queued IMU actions until the call returns. Face-down mute itself is scheduled onto this task. Connection failure also enters the global network-error path, which plays an error sound—an inappropriate side effect for a failed panel event. See [20-board-imu-gestures.patch:153](/Users/jwang14/Projects/weixue/firmware/patches/20-board-imu-gestures.patch:153) and [application.cc:190](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/application.cc:190).

**Fix:** Make event connection/send a bounded asynchronous operation with explicit transport ownership and cancellation. Return completion to the main task; do not merely move an unsynchronized `protocol_` call to another thread. Clean up partially opened transports and report failure on the card without triggering the voice-session alarm.

This was already identified by the gate and deferred in [tasks.yml:180](/Users/jwang14/Projects/weixue/tasks.yml:180); it remains in merged code.

**C2 — P2 · feat-ui-panel: “sent” ignores the actual send result**

Locations: [22-application-device-event.patch:117](/Users/jwang14/Projects/weixue/firmware/patches/22-application-device-event.patch:117), upstream [protocol.cc:102](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/protocols/protocol.cc:102), [monitor_panel.cc:465](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/monitor_panel.cc:465).

After checking `IsAudioChannelOpened()`, the code calls the void-returning `SendMcpMessage()` and unconditionally sets `sent = true`. That method discards `SendText()`’s boolean result.

**Scenario:** The socket fails between the open-channel check and the write, or the write itself fails. The callback still sets the card to “approve sent” and logs `Device event sent`, although no notification was transmitted.

**Fix:** Propagate transport success through the protocol API. For real approvals, separately track server acknowledgement using an event identifier; a successful local write cannot establish receipt or execution. The current fixture does not require HA execution acknowledgement, but its transport result must be truthful.

**C3 — P2 · feat-ui-ring-bedside: the clock and hint expiry do not follow corrected civil time**

Locations: [face_display.cc:840](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:840), [bedside_schedule.h:63](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/bedside_schedule.h:63), upstream [ota.cc:193](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32/main/ota.cc:193).

The OTA client adds a numeric timezone offset directly to the epoch before `settimeofday()`. The new display reads that clock with `localtime_r()`. The server supplies a configured numeric offset, not timezone transition rules: [ota_handler.py:224](/Users/jwang14/Projects/weixue-vendor/xiaozhi-esp32-server/main/xiaozhi-server/core/api/ota_handler.py:224).

**Scenarios:**

- A continuously powered device crosses DST: both the displayed clock and 22:00/07:00 transitions remain one hour wrong.
- A valid clock is subsequently corrected across a schedule boundary: an existing `night`/`day` hint still expires according to the old monotonic deadline.
- Without valid time, automatic bedside mode remains off; `--:--` is only visible if something else enables bedside mode.

The minute-window logic itself handles midnight and disabled windows correctly. The defect is the clock/expiry contract around it.

**Fix:** Keep UTC as the system clock, use an approved timezone rule, establish resynchronization and stale-time behavior, and calculate override expiry against the intended civil-time boundary. Reconnecting and fetching the same fixed offset alone will not fix DST. External time-service/configuration choices require Jay’s approval.

**C4 — P2 · feat-ui-ring-bedside: poll urgency is not bound to the delivered message**

Locations: [weixue_proactive.py:597](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:597), [weixue_proactive.py:392](/Users/jwang14/Projects/weixue/server/overlay/weixue_proactive.py:392), [21-ring-bedside.patch:96](/Users/jwang14/Projects/weixue/firmware/patches/21-ring-bedside.patch:96), [face_display.cc:869](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:869).

Polling returns the urgency of the current queue head. The later WebSocket invocation independently selects a queue item. Neither a message ID nor urgency accompanies actual playback.

**Confirmed in-memory scenario:** Non-urgent A has one second remaining when polled. Connection setup takes two seconds; A expires and urgent B becomes the selected item. Firmware retains `kProactive`, so B plays over the bedside page instead of showing the face. Reversing the priorities produces the opposite visual error.

**Fix:** Carry delivery ID and urgency with the actual announcement, before its playback begins. Alternatively, reserve the polled item and bind invocation to that reservation with explicit expiry/retry rules. A session-level boolean derived from an earlier queue snapshot is insufficient.

**C5 — P2 · feat-ui-panel: deliberate polling pauses become false “offline” reports**

Locations: [monitor_panel.cc:377](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/monitor_panel.cc:377), [22-application-device-event.patch:79](/Users/jwang14/Projects/weixue/firmware/patches/22-application-device-event.patch:79).

The panel treats a successful poll older than 90 seconds as offline. Polling pauses during conversations and microphone mute.

**Scenario:** Finish a conversation lasting over 90 seconds, or unmute after several minutes. The healthy WebSocket closes and the next long poll begins. Until that poll finishes—normally up to 25 seconds with an empty queue—the panel displays “offline” solely because the earlier observation aged while polling was intentionally suspended.

**Fix:** Represent `checking/reconnecting` separately from observed failure. Track why polling paused and when it resumed; preserve recent successful transport evidence without claiming that stale evidence proves an outage.

**C6 — P2 · feat-ui-panel: replies accumulate across unrelated sessions and announcements**

Locations: [monitor_panel.cc:319](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/monitor_panel.cc:319), [face_display.cc:418](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:418).

`reply_` is cleared only by a nonempty user transcript. Every assistant sentence appends to it, including wake acknowledgements and proactive announcements.

**Scenario:** After an answered question, a later wake cue or announcement arrives without a new STT message. The panel retains the old “Heard” text and appends unrelated speech to its “Reply”. Once the 240-byte cap is reached, new speech may not appear at all.

**Fix:** Associate displayed text with a turn/delivery identifier. Start a separate display record for unsolicited announcements and wake acknowledgements; only join sentences belonging to the same response.

**3. Security**

The fixture card does **not** implement HA approval. Its payload contains `fixture:true`; the server wrapper logs and forwards it. This matches the explicit fixture allowance in [DESIGN.md:81](/Users/jwang14/Projects/weixue/DESIGN.md:81). No new approval-execution bypass was found.

The new event log is nevertheless **untrusted telemetry**. [weixue_patches.py:245](/Users/jwang14/Projects/weixue/server/overlay/weixue_patches.py:245) accepts any method beginning `notifications/weixue/` and logs arbitrary parameters and the claimed device identity. Parameters are JSON-escaped, but the method is interpolated directly; there is no event schema, field allow-list, redaction or application-level size limit here. Before using these records for approval auditing, validate known events, escape all fields and bound retained data.

Wave 3’s unauthenticated device impersonation and notification-consumption finding remains open. The `urgent` field adds no authentication or ownership binding. Existing unauthenticated voice/HA exposure also remains unchanged; no security closure should be inferred from panel event logs. See [Wave 3 security findings](/Users/jwang14/Projects/weixue/docs/reviews/codex-gpt6-astra-high-wave3-code-review-2026-10-07.md:106).

**4. Reliability and rendering**

**Ring rendering is materially expensive, but the evidence does not establish a new audio failure.** Recorded final ring windows show listening at **32.7–38.1 ms average**, speaking at **28.2–30.3 ms**, and a **114.7 ms maximum**, versus the documented face-only 13–16 ms. These are refresh-start-to-ready measurements including flush, not isolated CPU or draw-callback timings. See [recorded frames:284](/Users/jwang14/Projects/weixue/docs/logs/ui-ring-bedside-c-device-2026-10-07.log:284) and [measurement implementation:1152](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:1152).

The implementation explains the cost:

- Twenty sector rectangles invalidate roughly 56k pixels for a solid-ring update, including substantial empty area around the thin band.
- The 100/66 ms limits throttle ring-driven invalidation, **not every invocation of its draw callback** during other intersecting redraws.
- Spinning arcs recompute sector geometry using trigonometry within draw callbacks.
- The 466×466 A8 mask costs about **212 KiB of PSRAM once at startup**. It is not allocated each frame.

Sources: [UpdateRing:1003](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:1003), [BuildRingMask:1046](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:1046), [DrawRing:1082](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:1082).

The clipping and stable image storage are sensible; no draw-descriptor lifetime defect was found. Optimize by caching sector geometry, reducing empty invalidated/blended area, and suppressing hidden/off-screen rendering work. Establish a release-build frame/input-latency budget and measure tails alongside audio health. The available evidence supports a performance concern, not a separate counted P1/P2 correctness finding.

**Panel/bedside/power behavior needs integrated acceptance.** Screen-off still blanks the physical panel while LVGL continues drawing. Touch while already lit updates bedside interaction time; touch from off deliberately only relights. IMU pickup/face-up likewise wakes the display without leaving bedside. A monitor panel remains above the face until explicitly closed or its inactivity timeout; entering bedside does not itself remove that overlay. These paths are visible in [21-ring-bedside.patch:236](/Users/jwang14/Projects/weixue/firmware/patches/21-ring-bedside.patch:236), [face_display.cc:464](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:464) and [05-board-power-usb.patch:54](/Users/jwang14/Projects/weixue/firmware/patches/05-board-power-usb.patch:54).

**The real touch path has an additional inherited failure mode.** The CST9217 driver returns an error for an invalid ACK or failed read; the LVGL port wraps that call in `ESP_ERROR_CHECK`, making a transient acquisition error fatal. The virtual pointer never exercises this path. This is inherited dependency behavior, not a newly counted Wave 4 defect. See [CST9217 driver:125](/Users/jwang14/Projects/weixue-vendor/fw-work/weixue/managed_components/waveshare__esp_lcd_touch_cst9217/esp_lcd_touch_cst9217.c:125) and [LVGL touch port:126](/Users/jwang14/Projects/weixue-vendor/fw-work/weixue/managed_components/espressif__esp_lvgl_port/src/lvgl9/esp_lvgl_port_touch.c:126).

**5. Test quality and hardware gaps**

The host tests cover useful pure logic: midnight wrapping, disabled schedules, hint durations, ring sectors and tile changes. They do not exercise LVGL rendering, clock synchronization, application scheduling, or transport completion. The hint tests assume an ideal relationship between civil minutes and elapsed seconds. [ring_bedside_test.cc:64](/Users/jwang14/Projects/weixue/firmware/tests/ring_bedside_test.cc:64).

The panel replay exercises actual LVGL hit testing and callbacks, but uses a **separately created input device**. It bypasses CST9217 interrupts, I²C acquisition, coordinate mirroring and the board’s `LV_EVENT_PRESSED` wake/bedside callback. Real input uses interrupt-driven event mode; replay uses the default pointer polling path. Physical gesture correctness therefore remains unverified. [monitor_panel.cc:480](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/monitor_panel.cc:480), [touch port:59](/Users/jwang14/Projects/weixue-vendor/fw-work/weixue/managed_components/espressif__esp_lvgl_port/src/lvgl9/esp_lvgl_port_touch.c:59).

The gesture callback and click guard are plausible, but tests cover a fast center-line swipe and centered taps. They omit slow/short/diagonal swipes, swipes beginning on buttons, long presses and interruptions during animation.

Still required on the final merged firmware:

- Finger swipe-left/right and Approve/Deny taps, including swipe-versus-click discrimination.
- Touch from dim/off, touch while already in bedside, and pickup/face-up relight at night.
- Wake or face-down mute during a stalled event connection.
- Failed writes, connection loss and repeated decisions.
- Schedule boundaries and urgency behavior while the panel is open.
- Physical flicker/tearing and input responsiveness during listening, thinking and playback.

These omissions are explicitly recorded in [docs/firmware.md:269](/Users/jwang14/Projects/weixue/docs/firmware.md:269) and [docs/firmware.md:284](/Users/jwang14/Projects/weixue/docs/firmware.md:284).

Final reconciliation **does have evidence now**: the log identifies merged firmware, patches 05–22, the rebuilt server image, overlay hash agreement and a live voice smoke. That closes the previous evidence gap about reconciliation, but not the feature-specific hardware gaps. [reconcile-wave4 log:1](/Users/jwang14/Projects/weixue/docs/logs/reconcile-wave4-2026-10-07.log:1).

**6. Spec drift and remaining Wave 3 findings**

| Requirement | Assessment |
|---|---|
| Ring/bedside feature row | Ring states and a shifted night window have device evidence. Reliable civil-time scheduling remains incomplete under C3. |
| Panel feature row | Fixture decisions satisfy the explicit scope clarification. Physical left/right swipe acceptance remains unproved despite `status: done`. |
| ADR-12 | USB survival, dim/off and wake-word relight have evidence. Combined physical touch/IMU/night behavior remains incomplete. |
| M2 | Static task chips and fixture cards are preparatory UI. They do not establish task state, durable delivery, approval ownership or execution. |
| Hardware/process gates | Voice smoke and virtual replay do not establish physical acceptance of the changed touch feature. |

Sources: [DESIGN.md:48](/Users/jwang14/Projects/weixue/DESIGN.md:48), [DESIGN.md:69](/Users/jwang14/Projects/weixue/DESIGN.md:69), [tasks.yml:81](/Users/jwang14/Projects/weixue/tasks.yml:81), [CLAUDE.md:40](/Users/jwang14/Projects/weixue/CLAUDE.md:40).

The [quality-log Wave 4 row](/Users/jwang14/Projects/weixue/docs/QUALITY-LOG.md:13) correctly says the gate *caught* clock and blocking-send problems. It should also record that they were **merged with fixes deferred**, as documented in [tasks.yml:180](/Users/jwang14/Projects/weixue/tasks.yml:180).

All **11 numbered Wave 3 runtime/security findings remain unresolved in this revision**:

| Wave 3 IDs | Still open |
|---|---|
| C1–C3 | Timeout-path microphone mute, one-breath capture loss, HA deadline/unknown-outcome handling |
| C4–C5 | Wake-cue ownership race and suppression of genuine short answers |
| C6–C7 | Proactive claim race and false successful-delivery reporting |
| C8–C9 | Twenty-second Thinking expiry and IMU polling-overrun assumptions |
| S1, R1 | Unauthenticated proactive consumption and false-positive bridge health |

The corresponding implementations were unchanged by Wave 4, apart from surrounding additions. Thinking still expires at 20 seconds in [face_display.cc:449](/Users/jwang14/Projects/weixue/firmware/overrides/main/boards/waveshare/esp32-s3-touch-amoled-1.75/face_display.cc:449), so the ring now inherits that misleading state transition. The broader Wave 3 concerns—ADR-11, in-memory proactive recovery, advisory device locking and incomplete gesture trials—also remain open. [Wave 3 review](/Users/jwang14/Projects/weixue/docs/reviews/codex-gpt6-astra-high-wave3-code-review-2026-10-07.md:1).

**7. Patch list ordered by value; quality-log counts**

1. **Patch 22 and protocol transport:** remove blocking event connection/send from the main task; add bounded cancellation, failure cleanup and truthful send results. **C1, C2.**
2. **Proactive protocol and patch 21:** bind urgency to the actual delivery ID and apply it before playback. **C4.**
3. **Clock/bedside integration:** implement approved timezone and synchronization semantics, including override expiry after clock correction. **C3.**
4. **Real touch validation and recovery:** exercise CST9217 input on the final build, add acquisition-error recovery, and verify bedside/screen-off/IMU interactions.
5. **Ring renderer:** cache geometry, reduce invalidated area and measure release-build frame/input latency alongside audio.
6. **Panel status/text model:** distinguish suspended/stale checks from failed connectivity and bind reply text to a turn. **C5, C6.**
7. **Acceptance records:** keep physical checks incomplete until observed; distinguish caught-and-deferred defects from fixes completed before merge.

Counts include distinct Wave 4 correctness findings, including defects already caught but deferred by the gate. They exclude inherited Wave 3 findings, unverified hardware failures and the unbudgeted rendering-performance concern.

| Task | P1 | P2 | Findings |
|---|---:|---:|---|
| `feat-ui-ring-bedside` | 0 | 2 | C3, C4 |
| `feat-ui-panel` | 1 | 3 | C1, C2, C5, C6 |
| **Wave 4 total** | **1** | **5** | **6 distinct findings** |