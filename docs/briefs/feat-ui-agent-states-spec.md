# Feature Spec: Agent states UI + management view with chats
Task: feat-ui-agent-states (covers the device side of feat-ui-sessions) | Branch: feat/ui-agent-states | Date: 2026-10-08

## Summary
The device now shows the agent task store. With tasks, the idle face shrinks above a complication with partial ring cues and per-tier effects; approval and clarification cards take the full screen. The left-swipe panel is now the management view: page 1 holds the current chat, tasks and chat switcher; page 2 the old panel plus Next up. Procedural earcons sound on arrivals and decisions.

## Changes
- `agent_ui.h`: pure model covering priority, status text, countdown, ring cues, the card state machine, auto-open by tier, rows and chats (built from the store).
- `agent_view.{h,cc}`: LVGL widgets. `face_display_agent.cc`: glue for the small face, gestures, responses and earcons.
- `voice_ring.h`: new `kArcs` style. `monitor_panel`: two pages, page dots and Next up; the fixture card is removed.
- `weixue_agent`: response callbacks and `Cancel`. `shown` is now sent at render time.
- Card decisions: one record per `card_id`, kept outside the card. It survives Later, blocks a second decision, and matches its transport callback by (card_id, send token). Taps use the card captured at open; a changed task refreshes the card instead of sending. 400 ms tap guard after a card appears; no auto-open under a finger. `still sending` after 4 s, `Not sent` only on outbox give-up or at 15 s; a retry resends the same decision.
- Gateway: first decision per card wins; later ones, retries included, are dropped as `duplicate decision`.
- Earcons: `firmware/tools/earcons.py` generates Ogg Opus 16 kHz into `agent_earcons.h` (6 sounds, 280–550 ms).
- Patch 32: `font_noto_sans_common_20_4.bin` is shipped in the assets partition.
- Tests: `agent_ui_test.cc`, host renderer `tests/render/`, fixture scenario `ui`; docs updated.

## Design Decisions
- Font (decision 26): measured at 1,287,460 B, under 1.5 MB, so it ships with the Chinese sublabels.
- Confirmation means the notification reached the transport (v1 has no decision ack). Voice-resolved cards show `Answered`.
- After `Not sent`, only the chosen control resends. A send cancelled at 15 s may still have reached the server, so the gateway's first-wins rule keeps a changed mind from landing as a second decision.
- No 8 s mic window after a chat switch, no countdown pause while you speak, no task-detail page.

## Problems & Resolutions
- Device `.bin` fonts are 32-bit, so they can't load on the host. Host renders use the compiled Latin fonts.

## Open Questions
- Should the chat list wait for a server `chats` index, which would bring real titles, or keep using the latest task title?

## Future Work
- Chats index frame; Stop/Retry detail page; `resolved_by` in the wire; the post-switch listening window.

## Test Coverage
Host tests pass, including `agent_ui_test` (all states and tiers, card flows, timeouts, chats, wire→UI, plus Later while sending, callback matching, captured-card taps, touch guard, and hard timeout/retry). The server suite passes, with per-card dedupe. Release, loopback and diag builds succeed (app 3.12 MB, assets 4.27 MB). Host renders of 14 screens light 1.4–12.2 % of the panel; only the new-chat transient reaches 17.8 % (≤ 8 s). **Deferred (board not on USB):** none of the following has been run on the device yet:
- flash and `ui` fixture run with the loopback build
- device lit counts and FACEDUMP screenshots
- real-finger swipes and taps
- the CJK font load
- earcon playback (home audio)
- release smoke
