# Board check: spoken = read (fix-terminal-items-polish, decision 84)

2026-10-12, diag build of `main` (PRs #33-#34 deployed 20:22Z), live stack, board on `/dev/cu.usbmodem2101`, `diag volume 20`. Lock held 20:27:16Z-20:52:16Z (holder expiry; the final flash-back took the lock again, 20:54Z-20:57Z). Times in the device logs are UTC; the gateway log is UTC+1 (`21:38:26` = `20:38:26Z`).

Result: steps 1-3 and 4 pass; step 5 is inconclusive (the speech started before the tap could land); step 6 done.

Files (this directory, prefix `spoken-read-check-2026-10-12-`; `FACEDUMP` lines removed, MAC/SSID/IP redacted):

| File | Content |
|---|---|
| `serial-boot1.log` | steps 1, 2, 4: flash boot, `diag volume 20`, the three API turns, `diag dump` |
| `serial-reset.log` | step 3: RTS reset of the same build (`serial_log.py --reset`) |
| `serial-deny-boot2.log`, `-boot3.log`, `-boot4.log` | step 5 attempts (the port open resets the board, so each run starts with a boot) |
| `serial-release.log` | step 6: release build booted |
| `gateway.log`, `agent-service.log` | `docker logs` of `xiaozhi-esp32-server` and `agent-service` on the server (over SSH), lines of the probe chats/tasks only |

Chats and tasks created (all new, non-current): C1 `cht_…VTKV9` / `tsk_…2NAC` (API turn), C2 `cht_…HWY` / `tsk_…JY` (`start_task` too short), C3 `cht_…R28Q` / `tsk_…D7CD3Y` (the spoken result), C4 `cht_…WRHMS8` / `tsk_…DZS013`, C5 `cht_…V4KA` / `tsk_…K3BEXWHPMD`, C6 `cht_…AG5E` / `tsk_…GC8G` (held-call probes).

## Step 1: build, flash, volume

Expected: diag build of main flashes, boots to idle, `diag volume 20` accepted.
Observed: `WEIXUE_FACE_DIAG=1 firmware/build.sh` exit 0, `firmware/flash.sh` exit 0 (hash verified, 3,146,480 B app), boot `unknown -> starting -> activating -> idle` (`serial-boot1.log` 5-8), `DiagConsole: ok diag volume 20` (L282) and `Set output volume to 20`. **Pass.**

## Step 2: a spoken WHEN_IDLE result is read

An API text turn alone is not a spoken delivery: its reply stream is the API reply, so the result is queued `silent` (C1, C2: device `tier=silent speaks=0`, then unread on the device; this is the spec's "reply to an API turn is not heard"). A spoken WHEN_IDLE result needs a task that outlives its reply: C3 asked for `start_task` plus four sequential web searches, with no text until done. Its reply ended at the hold timeout (`reply end … hold_elapsed`, `agent-service.log` L46), the task finished 20:38:23Z (`delivery queued … WHEN_IDLE agent`, L66).

| Expected | Observed |
|---|---|
| Result item `unread`, `tier=when_idle speaks=1`, spoken whole | `serial-boot1.log` L710 `state=unread … tier=when_idle speaks=1 result=applied via=poll`, L746 `<< Today is Monday`, L765 `agent played: … complete=1 played_ms=1080`, L767 `agent ack sent: played complete …` |
| Service log shows the `played` ack | `agent-service.log` L72-74: `delivery ack … spoken`, `… played`, `delivery seen … outcome played` (20:38:26.58Z); `gateway.log` L12 `agent item seen … seq=1791837506559 … : played`, then `agent push … seq=1791837506559` |
| Seen copy applies on the device with no arrival effect | L768 `state=unread seq=1791837506559 tier=silent speaks=0 result=applied via=push`; no `agent ui: task=<C3>` line follows (an unseen arrival logs one with flash/earcon, L713 for the first copy: `flash=2`); the audio channel closes and the device is idle at L774 |
| Chat read in the service | `GET /v1/chats/<C3>`: `"unread": false`; C1 and C2 (control, silent API replies): `"unread": true` |
| `diag dump` | L939-940: `device_state idle`, `ring=dot`. The dot is from other unread tasks (C1, C2 and five earlier ones, all unseen); `diag dump` does not list tasks, so "no dot" is not observable on the board while any other unread task exists. Per-task evidence is the missing arrival line and step 3's snapshot |

**Pass** (read by `played complete`; no arrival effect after the seen copy). Limit: the ring/dot is shared by all unread tasks, so the board cannot show C3's absence from it directly.

## Step 3: reset keeps it read

Expected: after a reset, the boot snapshot applies C3 as seen: no arrival line, not the top task.
Observed (`serial-reset.log`): RTS reset ~20:42Z (this file has no host timestamps); snapshot task lines applied L229-264. L263 `task=<C3> state=unread seq=1791837506559 … via=poll` (the seen copy's seq, so the snapshot carried `seen:true`); no `agent ui: task=<C3>` line follows, while each unseen task of the snapshot has one (L235-236, 244-245, 256-257 and 264: the older results, C1, C2). Top task after the snapshot: `pose=mini top=<C2>` (L275), not C3, which is the newer item. **Pass.**

## Step 4: which outcomes stay unread (control)

From the spec: `cut_off`, `lost`, `deferred`, `dropped`, unaccepted speech and `silent` results stay unread. Observed in the logs: C1 and C2, `silent` API replies, stayed `tier=silent speaks=0`, unread on the service (`unread: true`) and as `agent ui: task=… state=unread` arrival lines after each boot (`serial-reset.log` L257, L264). No `cut_off` or `lost` occurred. **Pass** (the `silent`/API-reply outcome only; the other outcomes are the service and gateway unit tests' job).

## Step 5: no card speech after a decision

Expected: Deny tapped as soon as the card opens, no approval speech afterwards; service log shows the speech withdrawn/dropped.
Observed: **inconclusive, no withdrawal case was produced.**

- The held call (`ha_call_service input_boolean.toggle input_boolean.weixue_session_probe`) opens a card at once (`SILENT`, `speaks=0`: for an API turn the cue counts as spoken inline, `interactions.py` `announce`); the speaking `WHEN_IDLE` card of the same interaction is queued only when ha-mcp returns the hold's token (`on_correlated`), 12 s later in C4 (`agent-service.log` 94 `delivery queued … WHEN_IDLE`).
- Attempt 1 (`serial-deny-boot2.log`, C4): Deny tapped 1.0 s after the first card opened (L307 `receipt: deny … result=stale`: the interaction was not yet actionable). The speaking card arrived 10 s later (L317) and played (L356 `<< I need your OK on the panel …`, L381 `played complete`). Not a withdrawal case: nothing was decided.
- Attempt 2 (C5, same hold joined as a waiter) never got a speaking card.
- Attempt 3 (`serial-deny-boot4.log`, C6, `turn_on` because the toggle hold was denied by then and an identical call is not held again): the driver waited for `state=needs_approval … speaks=1 result=applied` and tapped at once. The card arrived over an already open session, and the speech started in the same 100 ms (L561 `<< I need your OK on the panel to turn on …`, L565 `listening -> speaking`, L564 tap sent, L574 tap done, L587 `receipt: deny … accepted`). The speech was already playing when the decision was recorded, so per spec it finishes (`played complete` L618); nothing was withdrawn, nothing else spoke for that card afterwards (the denial results of C4-C6 were spoken as their own `unread` results). This matches the spec's "speech already playing finishes", not the withdrawal case.
- The withdrawal needs a speaking card the device has not yet started (device busy or not idle when the card arrives, then a tap). Unit and sim coverage of that case is in the spec's test list (gateway: "no withdrawal while speech plays" and withdrawal rows); the board did not exercise it. Suggested next try: keep the device non-idle (`diag press_boot`) until the speaking card arrives, tap Deny, then let it go idle and check for speech and the service log's `card=… withdrawn: card decided (deny)`.

## Step 6: release build back, clean-up

`firmware/build.sh` (no flag) exit 0, `firmware/flash.sh` exit 0, `strings build/xiaozhi.bin | grep -ci diag` prints 0, boot to `idle` (`serial-release.log`: `activating -> idle`, `agent: sync … tasks=8`, no `diag` or `FACEDUMP` line). Lock released, `status: free`.
All six tasks I created are `done` (none open): C4-C6 ended by the denied hold, C1-C3 finished on their own. No interrupt call was made. C1 and C2 stay unread cards (the silent API results), not tapped. The two denied holds (`toggle`, `turn_on`) stay recorded in ha-mcp. **Pass.**

## Findings

- Defect (test setup, not the feature): an API turn cannot produce a spoken WHEN_IDLE result; a task that outlives its reply (here web searches, 20 s) is needed. Worth a line in `docs/agent-protocol.md` or the spec's board-check recipe.
- The spoken approval card cannot be beaten by a tap on an idle device (speech starts about 2 s after the push and in the same 100 ms when a session is open); the withdrawal path needs a busy device.
- A held call identical to a denied one is not held again, so a second try needs a different service (`turn_on`).
