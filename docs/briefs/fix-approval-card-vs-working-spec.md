# Feature Spec: Open approval card survives a steer turn
Task: fix-approval-card-vs-working | Branch: fix/approval-card-vs-working | Date: 2026-10-13

## Summary
An open card (`needs_approval`/`needs_input`) now stays the task's device item for as long as its interaction is open. In the 21:22 round, a message to the waiting task set it back to `progress`. That turn's working item then replaced the card at the gateway and withdrew its speech, while the hold stayed pending.

## Changes
- Service: turns keep the card state. The task returns to `progress` only when its last interaction closes. No working item or activity label is sent while a card is open.
- Steer: the hold stays pending. Turns get a note that the action is waiting for approval and must not be repeated. An identical held call joins the pending hold, so there is one card.
- Gateway: a working item never replaces an open card unless a tap on this device decided it. The item is acked `dropped` and a reconcile runs, so a card closed by voice, the panel or expiry still gives way.
- Docs: agent-protocol, agent-service, server.md.

## Design Decisions
- The gateway checks closure with the existing reconcile instead of trusting the working item.

## Open Questions
- Correlation queues a second spoken cue for the same card. This was there before the fix.
- A steer answer that finishes after its reply closed, while a card is open, is not delivered. This was there before the fix.

## Test Coverage
- Service: 236 tests passed. The new `test_card_hold.py` fails on main.
- Gateway: 1047 tests passed.
- Recovery sim round `hold`: all checks pass. On main it reproduces the live lines (`docs/logs/fix-approval-card-vs-working-sim-2026-10-13.log`).

Deferred:
- Board check: waits for deploy, because the live stack runs main and no redeploy is allowed.
- Firmware host tests: no firmware change.
