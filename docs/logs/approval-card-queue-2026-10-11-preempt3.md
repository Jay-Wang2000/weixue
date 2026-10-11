# Approval gate, 2026-10-11 16:13 UTC

Build: 6620d7a diag. Held call: `ha_call_service` input_boolean.toggle on `input_boolean.weixue_session_probe`. `L<n>` = serial log line.

| Round | Card | Taps (responses sent) | Receipts | Earcons | End state (settled) | Replays | Result |
|---|---|---|---|---|---|---|---|
| P preempt | question tsk_01M4NVRR3PCZB79N5BD8K922Y7 L712; approval L1317; preempted L1315; question back L2098; question needs_input -> cancelled | approve (1) | accepted L2033 | needs_you L1316, approved L2037 | progress (done) | 1 | pass |
| A approve | L4150 | approve (1) | accepted L4241 | needs_you skipped: device busy or muted L3282, approved L4246 | progress (cancelled) | 1 | pass |
