# Approval gate, 2026-10-11 16:11 UTC

Build: 6620d7a diag. Held call: `ha_call_service` input_boolean.toggle on `input_boolean.weixue_session_probe`. `L<n>` = serial log line.

| Round | Card | Taps (responses sent) | Receipts | Earcons | End state (settled) | Replays | Result |
|---|---|---|---|---|---|---|---|
| P preempt | question tsk_01M4NVPJD9QKX8PZ4H7ARPXDNB L652; approval -; preempted none; question back L-; question progress -> cancelled | - | - | - | - (-) | - | no_hold: message routed to chat cht_01M4NVPJA10DF5Z4KWYNKHQ3V3 task tsk_01M4NVPJD9QKX8PZ4H7ARPXDNB (answer) |
| A approve | - | - | - | - | - (done) | - | no_hold: task done without a pending approval (ha_call_service denied: Home Assistant: the user denied this action. Do not retry it.) |
