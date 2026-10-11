# Feature Spec: ha-mcp client read timeout outlasts the hold
Task: fix-hamcp-client-timeout | Branch: fix/hamcp-client-timeout | Date: 2026-10-11

## Summary
A held ha-mcp call ended `result unknown (ReadTimeout)` at 15 s (live log: pending at 8002 ms, unknown at 15013 ms), before the 20 s hold returned its token. The client-wide httpx read timeout (15 s) overrode the adapter's per-call bound. Each request now carries its own timeout, and tool calls use `HA_MCP_READ_TIMEOUT_S` (default 35 s).

## Changes
- `weixue_hamcp/client.py`: each POST sets `httpx.Timeout(bound, connect=5)`. `timeout_s` is removed from the constructor and its callers.
- `config.py`: `HA_MCP_READ_TIMEOUT_S` (default 35). It must exceed the 8 s budget, otherwise the service refuses to start. `ha_adapter.py` uses it for calls and replays. A refresh logs `ha_timeout_config` when the policy `wait_seconds` is not shorter than the read timeout.
- Compose env, `.env.example`, `docs/agent-service.md`, `docs/server.md`.
- `scripts/audio_session.py` `wait_ready`: the per-state limit is 90 s while `speaking` and 30 s otherwise (`docs/firmware.md`).
- `server/sim/held_call_sim.py`: a held-call sim.

## Design Decisions
- The read timeout is a fixed setting and does not follow the policy's `wait_seconds`. A mismatch is logged rather than adjusted automatically.
- The ADR-9 8 s budget, the unknown outcome (no retry, identical write blocked) and `wait_seconds` are unchanged.

## Test Coverage
`tests/test_hamcp_timeout.py` covers:
- the setting and its validation;
- the per-request timeouts (read 42, connect 5, ping 10);
- a fake ha-mcp over real TCP that holds 20 s: pending at 8 s, owned token at 20 s, approve leads to exactly one replay;
- a fake that never answers: unknown at the read timeout, no retry, identical write blocked.

With the old client, the 20 s test fails exactly like the live run (unknown at 15002 ms).

Results: agent-service 158 passed; server 1031 passed; `scripts/tests/test_audio_session.py` 3 passed.

Sim log `docs/logs/fix-hamcp-client-timeout-sim-2026-10-11.log`:
- main: unknown at 15 s;
- branch: card approvable at 20.2 s, then approved and executed.

## Deferred checks
- Live deploy of main and the earcons approve + deny rounds on the device (orchestrator).
- No live redeploy, device use or Home Assistant write was done for this task.
