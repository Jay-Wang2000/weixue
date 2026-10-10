# Feature Spec: Agent message delivery defects (D1, D2) and cosmetics
Task: fix-agent-msg-delivery | Branch: fix/agent-msg-delivery | Date: 2026-10-10

## Summary
D1 was link loss, not parsing: one 1440 B TCP segment of a 2519 B body arrived, and the firmware read the empty result as "pending", opening a session with no lease. D2: the gateway closed that session inside its message loop, and the device counted "written" as delivered. Protocol v1.1 adds `agent_ack` receipts and a poll body cap.

## Changes
- Server: poll bodies of at most 2 entries and 1200 B. Snapshot pages the next header shows unapplied are resent (3 times max). Every response except `shown` gets a receipt, repeats the same one. A no-lease session hears no voice and closes after 2 s quiet (8 s max).
- Firmware: new pure `weixue_agent_outbox.h`: responses stay queued until their receipt arrives, resend with the same seq after 1/3/9 s, up to 4 attempts. The own session waits up to 2 s for the receipt. A short or invalid 200 body is a failed poll (retry after 1 s, no session). The card shows `Approve · sending` until the receipt, then `Approved`; hard timeout 30 s.
- Cosmetics: the card sub-line drops a chat or title that repeats the summary. A snapshot that drops a chat's tasks removes the chat from the pill and the list. State lines are fitted before LVGL, whose dots cut recoloured text early, so countdowns stay visible.
- Fixture `eight`, `closing`; sim weak link, no-lease invocation, delays. Loopback builds keep bedside off.

## Design Decisions
- Receipts go on the session that carried the response; resends cover a lost receipt.
- Rollout: a v1.1 device on a v1 server never gets receipts and ends at `Not sent`. Deploy the server first.

## Problems & Resolutions
- After a board reset, the previous boot's held poll took a snapshot page (the rewind recovered it). Replaced polls now answer 204.

## Test Coverage
Host: 450 checks pass. Server: 756 passed, 12 skipped (live). Sim: the D1 cut and the D2 loss reproduce on main and are fixed on the branch. On the board (loopback, then release, then main's release flashed back): `ui` 10/10, `states` 9/9 (release too), `burst`, 8-task snapshot after reset, and `closing` 3/3 with in-session receipts. Logs: `docs/logs/fix-agent-msg-delivery-2026-10-10*`.

## Deferred checks
- A no-lease session and a truncated body, on hardware (sim only).
- Device screenshots of the cosmetics and `Approve · sending` (host renders only; needs a diag build with diag_inject taps).
