# Approval gate, 2026-10-11 16:07 UTC

Build: 6620d7a diag. Held call: `ha_call_service` input_boolean.toggle on `input_boolean.weixue_session_probe`. `L<n>` = serial log line.

| Round | Card | Taps (responses sent) | Receipts | Earcons | End state (settled) | Replays | Result |
|---|---|---|---|---|---|---|---|
| A approve | - | - | - | - | - (cancelled) | - | no_hold: no pending approval within 90 s |
| C duplicate taps | L3185 | approve, approve (1) | accepted L3379 | needs_you L2676, approved L3388 | progress (cancelled) | 1 | pass |
| P preempt | question tsk_01M4NVBWMXFP4FG2JQ5SGNPSNV L-; approval not opened (open: None); preempted none; question back L-; question needs_input -> cancelled | - | - | - | - (-) | - | card_not_opened |
| B deny | L5794 | deny (1) | accepted L6503 | needs_you L5788, denied skipped: device busy or muted L6504 | done (done) | 0 | pass |
