# Feature Spec: fix-terminal-items-polish
Task: fix-terminal-items-polish | Branch: fix/terminal-items-polish | Date: 2026-10-12

## Summary
Decision 84: a spoken result is read. A result is seen once its speech is played whole, it is tapped, or it is heard in the device's own voice reply. The service saves this, so after a reboot the result does not come back as unread. Speech of a card that is decided or replaced is withdrawn before it plays (finding 76).

## Changes
- agent-service: a `played` or `user_ack` ack on a result card sets `seen:true` in the payload and marks the chat read. Later cards in that slot and the snapshot keep `seen:true`. A result heard inline in a voice reply is queued as seen. No schema change.
- Gateway: when a result is played, it is sent again with `seen:true` (silent, new seq). `ack_seen` marks the mirror entry seen. Card speech that has not started is withdrawn when the card is decided, replaced, reconciled or dropped (proactive `expired`, acked `dropped`).
- Firmware: no source change, because the device already treats `seen:true` as read. Host test added.
- Docs: agent-protocol.md, firmware.md, server.md.

## Design Decisions
- `cut_off` leaves the result unread; only `played complete` counts as heard.
- A reply to an API turn is not heard on the device, so the result stays unread.

## Problems & Resolutions
- Sim reboot sessions reused one boot, so their `played` events were deduplicated. Each session now gets its own boot.
- `mark_read` crashed on an unknown chat. It now ignores the call.

## Open Questions
- A played result marks its whole chat read, the same as a tap does. Another result in that chat that was never spoken still shows its own task chip.

## Test Coverage
- Service: 8 new tests (played, cut_off, lost, dropped, deferred, tap, voice reply, API reply).
- Gateway: 13 new tests (seen push, boot snapshot, ack_seen, withdrawal, and no withdrawal while speech plays).
- Firmware UI: 1 new test (seen copy, reboot snapshot).
- Sim: the seen round passes, as do all other rounds (`docs/logs/fix-terminal-items-polish-sim-2026-10-12.log`).
- Suites: server 1042 passed, agent-service 233 passed, firmware 0 failures.
- Deferred: the board check. The device is not on the Mac's USB, and the live gateway saw no device traffic. After the deploy, run it with the diag build: WHEN_IDLE result at `diag volume 20`, then `played`, no dot, serial reset, still read.
