# Approval card queue, board run 2026-10-11 16:00-16:16 UTC

Build: 6620d7a diag (`-preempt-diag-flash.log`), device lock 16:00:15-16:16:09. Live stack not redeployed: the gateway is main's (no approval tier floor, no boot ranking). Driver `scripts/approval_gate.py`. `L<n>` = line of that run's `-serial.log`.

Pre-step: `tsk_01M4NG76ATW0C5239WK83F8M1G` and `tsk_01M4M7YC4HJ6C16JZA4R3A1YTK` were `needs_input`, cancelled through `POST /v1/chats/{id}/interrupt` at 15:07:21 by this task's first run (`approval-card-queue-2026-10-11-result.json`); both read `cancelled` before round A of `-preempt` (`--cancel-task`, action none).

| Round | Run | Card | Taps (responses) | Receipt | Earcons | End state (settled) | Replays | Result |
|---|---|---|---|---|---|---|---|---|
| A approve | `-preempt3` 16:13 | L4150 | approve (1) | accepted L4241 | needs_you (skipped, busy) L3282, approved L4246 | progress (cancelled) | 1 | pass |
| B deny | `-preempt` 16:06 | L5794 | deny (1) | accepted L6503 | needs_you L5788, denied (skipped, busy) L6504 | done (done) | 0 | pass |
| C two taps | `-preempt` 16:04 | L3185 | approve, approve (1) | accepted L3379 | needs_you L2676, approved L3388 | progress (cancelled) | 1 | pass |
| P preempt | `-preempt3` 16:12 | question L712 (face tap), `card preempted` L1315, approval L1317, question back L2098 (`reason=return`) | approve (1) | accepted L2033 | needs_you L1316, approved L2037 | approval progress (done), question needs_input (cancelled) | 1 | pass |

Rounds not counted:

- `-preempt` A (16:03): the probe message was routed by agent-service as the voice answer to an `ask_user` question of another chat (a household voice turn at 16:02 had left a 20 s voice window open). No hold. That driver then cancelled the task the message landed in (`tsk_01M4NSX072C8JP92PGWJES5QW4`, chat "Jarvis. Jarvis."); the driver now refuses to cancel outside probe chats and reports such a message `no_hold: message routed to chat ...`.
- `-preempt` P (16:05): the `ask_user` question card arrived `silent` (spoken inline into the API reply), and after the 20 s voice window the turn's next item (`working`) replaced it on the device while the task stayed `needs_input`. No card opened: `card_not_opened`.
- `-preempt2` P (16:11): the held-call message was taken as the answer to the probe question (its voice window). `-preempt2` A: ha-mcp answered the call identical to B's denied one with `USER_DENIED` at once. `-preempt3` uses `--held-entity input_boolean.weixue_session_probe_p` (also absent from Home Assistant) and waits 22 s after the question card opens.

Restore: main's release build (main 974c9c4; its provenance matched) flashed (`-preempt-release-flash.log`); boot to idle at 5.8 s, 0 `DiagConsole` and 0 `diag`/`FACEDUMP` lines (`-preempt-release-boot-serial.log`), 0 `DiagConsole` strings in the flashed app image.

Household speech in `-preempt-serial.log` is redacted. MACs redacted; no tokens or ha-mcp URLs in the logs.
