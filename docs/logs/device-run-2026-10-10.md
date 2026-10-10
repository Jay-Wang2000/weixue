# Device run 2026-10-10: deferred on-device checks

Board `esp32-s3-touch-amoled-1.75c` on `/dev/cu.usbmodem2101`, home LAN, live server from `main` (docker compose, `xiaozhi-esp32-server` image `:local`), main checkout at `2caf116` (HEAD moved to `6afeedb` by docs-only commits during the run; `git diff 2caf116 HEAD -- firmware scripts vendor.lock` is empty, and every `flash.sh` passed its provenance check). Device lock held from 20:42:13Z (before the first flash) to the end of the last on-device capture, released. No code committed or edited; only `docs/logs/` written.

Builds (all `exit=0`, `build-flash.log`): loopback 136 s / `xiaozhi.bin` 3,123,168 B; diag+loopback 84 s / 3,129,056 B; release 131 s / 3,122,400 B. Assets 4,273,502 B. Each flashed with `firmware/flash.sh` (hash-verified).

Log files (`docs/logs/device-run-2026-10-10-*`). Line numbers below are lines of these files. MAC, IPs, SSID, UUIDs, URL paths and tokens are redacted; server times are local Europe/Dublin (UTC+1), serial times are device uptime; every serial capture starts with a reset (opening the port resets the board, so each capture is also a board reset).

| Tag | File |
|---|---|
| `bf` | `-build-flash.log` |
| `S1` | `-1-loopback-boot-serial.log` (step 1, 60 s) |
| `S2` | `-2-fixture-serial.log` (loopback, four captures concatenated) |
| `S3` | `-3-diag-serial.log` (diag+loopback, two runs, FACEDUMP lines removed) |
| `S4` | `-4-release-boot-serial.log` (step 3, 60 s) |
| `S5` | `-5-release-states-serial.log` (release, `states` fixture) |
| `SV` | `-6-server.log` (server log, filtered) |
| `FX` | `-7-fixture-script.jsonl` (fixture output, 76 item records) |
| PNG | `-png/diag1-*`, `diag2-*` (14 FACEDUMP decodes) |

## Step 1 and step 3: boot checks

| Check | Result | Evidence |
|---|---|---|
| Loopback boot: Wi-Fi, OTA, tz, idle | pass | S1:179 `Connected to WiFi`, S1:189 `Clock: TZ IST-1GMT0,M10.5.0,M3.5.0/1, UTC offset +60 min`, S1:192 `Activation done`, S1:194 `activating -> idle`, S1:201 one `Model 0: wn9_jarvis_tts`, S1:1 and S1:111 `loopback build, answers debug_reply after 2 s; output volume 0`; OTA served by the server: SV:10-16 (`固件已是最新: 2.5.1`) |
| Loopback idle 60 s | pass | S1:219 `agent: sync ... tasks=0 via=poll`, polls re-established every ~27 s, no alert, no `E (` line besides one `TCP receive failed` at 5.2 s (S2:185) |
| Release boot: OTA, tz, idle | pass | S4:173 Wi-Fi, S4:183 tz line, S4:186 `Activation done`, S4:188 `activating -> idle`, S4:194 one wake word model; no `diag`/`FACEDUMP` lines |
| Release: proactive poll | pass | S4:189 `Proactive poller: http://<IP>:8003/<path>`, S4:180,228,242,270,277 new poll connections, S4:229-236 items applied via poll |
| ASR provider | pass (config + init, no STT turn run) | SV:3 `初始化组件: asr成功 GoogleSTTStream` (server start, 21:35:48), live `data/.config.yaml` `selected_module` has `ASR: GoogleSTTStream`; no voice turn in this run, so no per-turn STT line |
| Release smoke | pass | S5: 9/9 `states` items applied and `shown`; earcons `needs_you` (S5:275) and `done` (S5:289) logged without `skipped`; no crash/reboot |

## Deferred checks: feat-protocol-agent-msg-spec

| Deferred check | Result | Evidence |
|---|---|---|
| Flash LOOPBACK, boot, OTA | pass | above |
| `states` on hardware: every state applied and drawn (`shown`) | pass with a defect (D1) | `ui` 10/10 `shown` (FX:1-24, device S2:218-300); diag-run `states` 9/9 `shown` (FX:165-186, S3:535-954), release `states` 9/9 (FX:187-208, S5). First `states` run on the loopback build: 3 of 9 `shown`, the rest delivered 2 min later by push (S2:360-411, SV:85-111) |
| `approval`, `clarify` loopback answers reach the gateway | pass | `approve` SV:150, SV:169; `answer` SV:136, SV:186 (`via=loopback`), device S2:396, S2:424, S2:521; `ack_seen` (unread scenario) SV:159-160 with `user_ack` recorded |
| burst: duplicate, stale, oversize | pass | S2:654-660: `applied`, `dropped poll item of 2236 bytes` (limit 2048), `duplicate`, `applied` (newer), `stale`, `applied`; FX:97-108. First burst ran while the store held 8 open cards: all items acked `dropped` (`device task store full of open cards`, SV:189-196), as designed |
| snapshot rebuild after a board reset | pass | reset with tasks in the mirror: S2:570 `sync listed=8` (10 s after boot), S2:913 `sync listed=3`, S2:1436 `sync listed=7 dropped=1` after `drop` (SV:377 `tasks dropped ... snapshot due`) |
| busy→idle redelivery timing | pass | `busy`: session idle at S2:1091 (88.097 s), next proactive message pending S2:1098 (98.907 s): 10.8 s; `spoken`: idle S2:992 (53.697 s) to next message S2:999 (64.197 s): 10.5 s |
| `played` complete acks (output volume 0) | pass | S2:985, 1035, 1134 (`complete=1 played_ms=1440/2160/2280`), SV:264-265, 292-293, 352-353, FX:115-122 (`speeches` state `played`) |
| `played` cut (`cut_at_ms`), `cut_off` by wake word or IMU | skip | needs a wake word or a gesture during playback; no `say`, no person-driven gesture in this run |
| Per-card decision dedupe at the gateway | pass | `duplicate decision (first approve boot=... seq=3): dropped` SV:177 (second item on the same card), SV:225, SV:374, SV:389 (loopback re-answers after resets) |
| Home audio gate: spoken WHEN_IDLE/INTERRUPT audible, gateway restart after unacked speech → `uncertain` | skip | loopback plays at volume 0, release run did not send speech; audibility not checked; gateway restart not run |

## Deferred checks: feat-ui-agent-states-spec

| Deferred check | Result | Evidence |
|---|---|---|
| Flash and `ui` fixture on the loopback build | pass | FX:1-24 (10 items, all `shown`), S2:218-300; second `ui` run on the diag build FX:141-164, S3:1-533 |
| Device lit counts and FACEDUMP screenshots | pass (partial coverage) | 15 dumps, all lit shares ≤ the host renders' 12.2 % maximum: approval card 8.38 % and 8.70 % screen (10.67 % / 11.08 % circle; S3:266, S3:800), card 5.74-6.07 %, input card 4.28 %, management page 1 3.98-4.18 %, page 2 7.76 %, `happy` pose 4.45 %, `mini` small face 2.78 %, idle (sleepy 5.79-6.03 %, neutral 6.23-6.43 %); S3:266-532, S3:800-937. PNGs: `diag1-face-001/002-card` (approval card, Chinese sublabels rendered), `diag2-face-004-card` (question card with ①②), `diag1-face-003/005-panel` (management page 1 and 2), `diag2-face-005-happy` (unread result, `Done · tap to hear`), `diag2-face-006-mini` |
| Face-state coverage of the tour | partial | the bedside page took the face at 22:00 local (S3:280 `bedside=on reason=night local=22:00`), so `when_idle` items showed no small-face complication or card afterwards (`earcon=needs_you skipped: bedside`, S3:324, S3:332); `interrupt` items left the bedside page and opened cards (S3:848-849). Pose dumps exist for `happy` and `mini` only; working/failed/stopped poses were applied (S3:535-954) but not dumped |
| Real-finger swipes and taps | partial | taps: three `ack_seen via=tap` from a person tapping unread results in the release run (S4:247-266, 12-17 s); swipes: only the diag virtual-pointer replay (`MonitorPanel: page=panel/2/1/face`, S3:303-334, S3:414-430); finger Approve/Deny taps and real swipes not run |
| CJK 20 px font load | pass | S1:95 `agent ui: 20 px CJK font loaded (1287460 bytes)`; `待批准 / 问题 / 批准 / 拒绝 / 任务 / 家` rendered in the PNGs |
| Earcon playback | pass in log, not heard | release: `earcon=needs_you` S5:275 (interrupt card), `earcon=done` S5:289, no `skipped`; loopback and diag builds play at volume 0; no listener confirmation |
| Release smoke | pass | see step 3 |

## Defects found (not fixed)

**D1. Poll responses with several items are lost on this link; delivery falls back to a proactive session two minutes later.** In the first loopback capture the device stopped applying poll bodies after the 3rd `states` item: `TCP receive failed: -1` (S2:334, 349), `Wait for HTTP headers receive timeout` (S2:337, 351), and `Connection closed prematurely, expected 2519 bytes but only received 1260 bytes` (S2:361). The server had logged `poll body ... items=3 / 7 / 8` at 21:46:40, 21:47:25 and 21:48:15 (SV:85, 94, 105) and no ack followed. At 21:48:45 the device opened a proactive session (`Proactive message pending (urgent=0)`, S2:363) and received everything by push. Bodies of 1-2 items always arrived; a later 2,236-byte item arrived whole (S2:655). Mac-to-device ping at that time: 0 % loss in one run, avg 468 ms, max 1,144 ms, and 100 % loss in an earlier run, so the link is weak; whether the loss is the link, the device's HTTP read or the server's response write is not established.

**D2. A decision sent in a session the gateway is closing is lost, and the device reports it as sent.** In that fallback session the server logged `proactive session opened with no current lease for it; closing` (SV:119) at 21:48:45 and disconnected at 21:48:55. Between those, the device sent `shown` seq 1-2 and the loopback `approve` seq 3 for card `i_214711_ap` on the audio channel (`Device event sent (audio channel)`, `agent ack sent: approve ... (attempt 1)`, S2:396-398). The server logged no `agent response` for it: the same card's approve after the next reset (boot 2, seq 1) was accepted, not `duplicate decision` (SV:149-151), which shows the first never arrived. A real tap in that window would show `Approved` and never reach the gateway (v1 has no decision ack).

Cosmetic: the card's sub-line repeats the summary when the task has no title (`Turn on the hall light · Turn ...`, `Which lights? · Which lights?`; fixture items carry no `title`); the management page's current-chat pill kept `Leak sen...` (title of a dropped task) after the mirror was cleared (`diag1-face-003`); the countdown in page-1 state lines is cut (`Needs your OK · 1:...`, `diag1-face-005`).

## Notes

- After each device reset the loopback build re-answers the latest unreplied item of a card (`debug_reply` rides the snapshot); the gateway drops it as a duplicate decision (SV:225, 374, 389). That is how the per-card dedupe was exercised; a release build never does it.
- The gateway mirror holds 8 open cards; the `ui` tour leaves 3 approvals and 1 question open, and repeated tours fill the store, so I dropped the mirror between runs (`FX` `drop_all` records).
- Device volume was left at the default; macOS `say` not used; no secrets printed (the fixture token was read from `server/.env` by the wrapper and never echoed; logs checked for it).
