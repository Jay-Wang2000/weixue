# Feature Spec: Remove debug_reply from every build
Task: fix-remove-debug-reply | Branch: fix/remove-debug-reply | Date: 2026-10-11

## Summary
No build and no server message can answer an agent card (decision 53, ADR-13 unconditional). Card decisions leave the device only from a tap; test runs answer cards on the diag build over USB with `diag card`, a pointer tap like a finger.

## Changes
- Firmware: `debug_reply` parsing and the loopback auto-answer removed; `build.sh` refuses `WEIXUE_AGENT_LOOPBACK` and fails a build whose `xiaozhi.bin` contains `debug_reply`.
- Diag build (patch 33, `diag_card.h`, `AgentView::CardControlCenter`): `diag card approve|deny|later|option <n>` taps the centre of the open card's control, using its live coordinates. `diag volume <n>` (this boot only) and `diag bedside <hint>` take over the loopback build's silent, bedside-off boot.
- Server: schema refuses `debug_reply` (unknown field), `via` is `tap` only; gateway `replied`/replay logic removed; fixture field removed; sim device `--tap approve|deny` replaces loopback.
- `scripts/audio_session.py` card_tap uses `diag card approve`. Docs updated: agent-protocol.md and firmware.md.

## Design Decisions
- The loopback flag is removed entirely: without auto-answer a fixture card needs the diag console anyway; volume 0 and the day hint became diag commands.
- `diag card` injects a tap at the control's LVGL coordinates rather than calling the handler, so finger guards apply.

## Problems & Resolutions
- Without `debug_reply`, a valid item cannot reach the 1024 B limit (the largest is 854 B). The size test now lowers the limit with monkeypatch.

## Open Questions
- None.

## Future Work
- Run `render.sh` in CI. It now checks the card control centres, but `run.sh` does not include it.

## Test Coverage
Firmware `run.sh` passes: new resolver tests (agent_ui_test) and source checks (no scripted answer; responses only from UI/AckSeen; `diag card` is a pointer tap); render.sh checks card control centres. Server: 968 passed. New tests show the schema refuses `debug_reply`, the gateway drops `via:loopback`, no fixture scenario sends the field, and the sim device taps. On the board (diag, volume 0): `diag card approve` produced `approve via=tap`, which got a server receipt (`stale`, since agent-service has no fixture chat). The release build was then flashed back. Strings: `debug_reply` 0 in both builds. Deferred: the live gateway still runs main's image, so the server refusal is shown by unit tests only until the next deploy. No audio was needed.
