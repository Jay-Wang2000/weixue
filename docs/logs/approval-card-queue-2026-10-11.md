# Approval gate, 2026-10-11 15:36 UTC

Build: 2652b6f diag. Held call: `ha_call_service` input_boolean.toggle on `input_boolean.weixue_session_probe`. `L<n>` = serial log line. Run 15:07 UTC with the driver of 2652b6f; replays and verdicts recomputed from `-agent-service.log` and `-result.json` with the final driver (the run's driver counted B's `ha call finished` line with outcome `denied` as a replay and required played earcons). Round C here met B's denial (the identical call came back denied at once, no hold); C ran again at 15:33 UTC: `approval-card-queue-2026-10-11-C.md`.

| Round | Card | Taps (responses sent) | Receipts | Earcons | End state (settled) | Replays | Result |
|---|---|---|---|---|---|---|---|
| A approve | L723 | approve (1) | accepted L1369 | needs_you L722, approved L1376 | progress (cancelled) | 1 | pass |
| B deny | L2239 | deny (1) | accepted L2799 | needs_you skipped: device busy or muted L1985, denied skipped: device busy or muted L2800 | done (done) | 0 | pass |
| C duplicate taps | - | - | - | - | - (done) | - | no_hold: task done without an approvable hold |
