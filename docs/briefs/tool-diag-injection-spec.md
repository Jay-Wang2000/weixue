# Feature Spec: Diag injection over USB/MCP + scripted device session
Task: tool-diag-injection | Branch: tool/diag-injection | Date: 2026-10-10

## Summary
The diag build now accepts gestures, touches and BOOT clicks from the USB serial console (`diag ...` lines) and two device MCP tools, through the same handlers as the sensor, the touch panel and the button. `scripts/audio_session.sh` runs the voice + physical session unattended and writes a pass/fail table. The release build carries none of it.

## Changes
- `diag_input.h`: `DiagPointer` (second LVGL pointer; queued taps/swipes) and `DiagConsole` (reads the USB Serial/JTAG RX FIFO, answers `DiagConsole: ok|error`).
- Patch 33: `diag gesture|tap|swipe|press_boot|touch tour|dump|help`; `self.diag.gesture`, `self.diag.touch`; `OnBootClick`/`TouchPressedCb` shared with the real inputs.
- `FaceDisplay::DiagStateJson/DiagRequestDump`; the periodic 120 s panel replay removed (now `diag touch tour`).
- `firmware/tools/diag_inject.py`: one port owner (`follow`/`--follow` with log, script, control socket; `send`). Host tests `test_diag_inject.py`.
- `scripts/audio_session.sh` + `.py`: lock, diag flash, items, server log, release flash; volume 55 set/restored; audio refused outside home mode.

## Design Decisions
- Injected gestures call `OnGesture` directly (no fake `IMU tap:` register line); everything after it is identical.
- The virtual pointer registers the board's touch-press handler, so OnTouch/display wake behave as for a finger.

## Problems & Resolutions
- `serial.flush()` blocks when the device does not read (release build); removed, write timeout kept.

## Open Questions
- None.

## Future Work
- `card_tap`/`earcons` need the fixture token; the orchestrator passes it for the real session.

## Test Coverage
Host: `firmware/tests/run.sh` passes (28 Python tests incl. 5 new). On the board, `audio_session.sh --no-audio` (docs/logs/session-2026-10-10-2107.md): boot, face_down 10/10, double_tap 10/10, press_boot, panel 6/6, pick_up_dim 10/10 pass; event lines match handson-2026-10-07 and simulate_face logs; release `xiaozhi.bin` has 0 `diag` strings and is back on the device. Deferred to the orchestrator's audio session (night quiet mode, no `say` here): one_breath ×10, wake_cue, long_reply, barge_in, double_tap during TTS, wake while face-down, press_boot conversation, bedside_voice, card_tap + earcons (fixture), the MCP diag tools (need a server call).
