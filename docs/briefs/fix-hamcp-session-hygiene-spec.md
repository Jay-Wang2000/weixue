# Feature Spec: ha-mcp session hygiene in the bridge
Task: fix-hamcp-session-hygiene | Branch: fix/hamcp-session-hygiene | Date: 2026-10-10

## Summary
The bridge now holds one MCP session per process, answers endpoint `initialize`/`tools/list` from a cached tool catalog, bounds every exchange, and ends sessions with `DELETE`. The session-accumulation hypothesis is refuted for ha-mcp itself: its embedded server runs `stateless_http=True` (no `Mcp-Session-Id`, `DELETE` → 405), and 20 bounded read-only cycles on the live server showed no slowdown.

## Changes
- `hamcp.py`: `ensure_session()` (one initialize per process; 404 reopens), `terminate()` DELETE, whole-exchange bounds (tools/list 20 s, initialize 15 s, ping 10 s, calls 120 s), per-method first-byte/total timings.
- `catalog.py` (new): on-disk cache `data/mcp-bridge/cache/ha-mcp-tools.json`, shared in-flight refresh, background loop (15 min; 5–300 s backoff), re-register only on change.
- `bridge.py`: endpoint reconnects reuse the session; health = announce + ping; `ha-mcp stats` key=value line every 10 pings; SIGTERM ends the session.
- Compose cache volume; `docs/ha-mcp.md`; brief §8 session-discipline paragraph.

## Design Decisions
- Endpoint `initialize` is never forwarded, so device sessions cannot open more ha-mcp sessions.
- Cache stores the unfiltered list; the voice filter is applied on use.

## Problems & Resolutions
- Root cause of the 2026-10-07 hang is still unknown. All three occurrences were in tunnel mode; local stalled-reader tests did not reproduce it. Upstream issues #2367/#1644 describe a similar intermittent HTTP hang. The bridge now stays registered and healthy on the cache while it recurs.

## Open Questions
- (Jay, ha-mcp config) Enable ha-mcp's opt-in "HTTP transport diagnostics" (no payloads logged) so the next hang is diagnosable? Options: A diagnostics only (recommended), B also JSON responses instead of SSE, C neither.
- Merge order: feat-agent-service's WIP moves `hamcp.py` into `server/weixue_hamcp`; the second merge must port `ensure_session`/timings.

## Deferred checks
- Live deploy (orchestrator): bridge log shows `session stateless`, `tools/list ok … first_byte_ms`, `ha-mcp stats` after 5 min; cache file written; `check_ha_path.py` passes.
- Tunnel-mode run; next real hang shows the bridge staying healthy.
- No voice end-to-end run (night quiet mode).

## Test Coverage
107 bridge unit tests pass (suite: 739 pass, 1 pre-existing `test_voice_policy` failure also on main). A fake ha-mcp records sessions/DELETEs: 50 reconnects share one session; 50 run() restarts open and delete one each. Also covered: stalled-body timeout, cache-only registration while tools/list hangs, backoff. Scratch: 50 SIGTERM restarts against a stateful MCP SDK server left 0 open sessions (main leaked 50).
