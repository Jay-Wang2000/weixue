# Feature Spec: Server fixes from Codex Wave 5 review
Task: fix-wave5-server | Branch: fix/wave5-server | Date: 2026-10-07

## Summary
Gateway-side fixes for review items C1-C7, T1 and R2 (R1 stays an accepted risk). Delivery outcomes now come from packets actually written, the echo guard keeps corrections, the wake cue respects aborts, invocations consume only their own lease, and a deploy commits only after a read-only HA call succeeds on the voice path behind a fail-closed tunnel.

## Changes
- C1: wrapped `_do_send_audio` counts written packets; send errors/cancellation recorded; LAST completes only fully drained and still owned; pre-audio errors retry (`send_failed`, `error`).
- C2: per-caption written-audio interval; every non-filler word must match one contiguous run (negation/action/target/value differences barge in; EN+ZH tests).
- C3: event generation (abort, wake, transcript, session end) fences the cue after every await; never clears a newer abort; releases cue speaking state.
- C4: poll returns `lease`; one lease per device; bound (`weixue:proactive:<id>:<lease>`) or bare-but-issued-before-session invocation; expired/stale consume nothing.
- C5/C6: `setup.sh` gates `.env.deployed` on banners, wg health, bridge health and `check_ha_path.py` (`ha_get_state zone.home` from the server container); endpoint key + URL + both consumers roll back together.
- C7: `WG_BOUNDARY` drops AllowedIPs traffic not via wg0, installed before `wg-quick up`; full-tunnel configs refused; bridge waits for healthy wg and `wg0`.
- R2: health refreshed only on a matching ping result. T1: asserting sim gate (46 checks).

## Design Decisions
- Bare invocation kept for current firmware, bounded by lease-issued-before-session-open.
- Probe entity `zone.home`; `ENTITY_NOT_FOUND` also counts as HA answering.

## Problems & Resolutions
- Old sim echo scenario scripted the next sentence's text; the realistic playing-sentence echo is used.

## Open Questions
- Refusing default-route AllowedIPs follows the documented split-tunnel rule; confirm.

## Future Work
- Firmware sends the lease-naming invocation (with feat-protocol-agent-msg).

## Test Coverage
633 unit tests pass (C1-C4, R2, fake-docker setup readiness/rollback, wg boundary, compose). Sim gate 46/46 (`docs/logs/fix-wave5-server-sim-2026-10-07.log`). C7 scratch containers show drops on boot failure, interface removal and namespace replacement (`docs/logs/fix-wave5-server-c7-tunnel-boundary-2026-10-07.log`). Deferred to the orchestrator live step: `setup.sh` in tunnel mode (readiness lines, `WG_BOUNDARY` present, bridge healthy), proactive announce/conversation from idle with current firmware, one HA light action (`light.living_room_ceiling_lights`), and at home the audio checks: 60 s reply without self-abort, spoken correction during playback, abort during cue, all three cues followed by a short answer.
