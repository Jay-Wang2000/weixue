# Feature Spec: Operator-only device tools (ADR-13)
Task: fix-device-tool-exposure | Branch: fix/device-tool-exposure | Date: 2026-10-11

## Summary
Every device MCP tool now has a class. Agent-visible tools are get_device_status, set_volume, set_brightness, set_theme, screen.set_bedside and imu.get_state. Every other name is operator-only: it is never listed and `tools/call` refuses it. The `self.diag.*` MCP tools are gone, so input injection works only through the diag build's USB serial console. The server enforces the same whitelist as a second line.

## Changes
- `weixue_mcp_policy.h`: the allowlist and `ClassifyMcpTool`. It fails closed: any name not on the list is operator-only.
- Patch 34 (vendored `mcp_server`): `tools/list` returns agent-visible tools only, whatever `withUserTools` says. A `tools/call` for an operator-only tool gets error -32601 and logs `W MCP: tools/call refused: <name> is operator-only`. Boot logs show each tool's class.
- Patches 28/33 regenerated: `simulate_face`, `diag.gesture` and `diag.touch` removed.
- `server/overlay/weixue_device_tools.py`: the server drops non-visible tools from the device's tools/list and refuses calls to them, logging both.
- Docs: firmware.md, agent-protocol.md, server.md.

## Design Decisions
- The allowlist fails closed, so the upstream user-only tools (`reboot`, `upgrade_firmware` with a URL, `snapshot`, `assets.set_download_url`) and `reconfigure_wifi` are now operator-only too.
- The refusal reply does not echo the tool name, so the JSON reply is never built from untrusted text.

## Problems & Resolutions
- The rebase onto main brought in firmware changes from PR #4, so both builds were rebuilt and the smoke test was run again on the rebased code.

## Open Questions
- The `WEIXUE_AGENT_LOOPBACK` test build still honours a server-scripted `debug_reply` (approve) on agent items. Release builds ignore it. Keep or drop?
- Board state: it now runs this branch's release build, which is main plus patch 34.

## Future Work
- When the gateway adds `device_action`, it must check `is_agent_visible`.

## Test Coverage
Host: `mcp_policy_test.cc` compiles the patched vendored `mcp_server.cc` against stubs. It checks tools/list (with/without `withUserTools`, cursor), refused calls for diag, wifi, camera, reboot, upgrade and an unknown lamp tool, accepted calls, and the registration log. It fails 25 checks without the patch. Server: 24 new tests, including a check that the server list equals the firmware header (784 passed). On the board: the diag build's tools/list from the device holds the 6 tools (server log); `diag gesture face_down/face_up` over USB works; the release build has 0 `diag` strings. Deferred: on-device evidence of a refused call. No server path can send a forbidden tools/call today; the host test covers it.
