# Feature Spec: agent service on the in-process loop (M2)
Task: feat-agent-service | Branch: feat/agent-service | Date: 2026-10-11

## Summary
`server/agent-service/` now runs one asyncio process with an append-only Claude conversation per chat on the Anthropic SDK 1.11 tool runner: quick asks on Sonnet 5.5 (effort low), tasks on Fable 5.1 (effort high) after `start_task`, all 77 ha-mcp tools behind bm25 tool search through the ADR-9 `HaGateway`. S0/S1 passed (`docs/logs/feat-agent-service-s1-2026-10-10.md`); deployed live via `setup.sh`; live e2e passed.

## Changes
- `actor.py` (runner loop, boundary steer, cancel), `transcript.py`, `models.py`, `app_tools.py`, `memory.py`, `prompts/system.md`; `turns.py` engine rewritten around them; SDK modules, workspace and fake SDK removed.
- Store: `transcript`, `chat_prompts`, epochs/modes (22 tables); `weixue_hamcp` gets the bridge's session discipline.
- Compose service, `setup.sh` (token generation, readiness, first-deploy rollback), xiaozhi gets `AGENT_SERVICE_URL/TOKEN`; `docs/agent-service.md`, `docs/server.md`.
- `tools/spike_s1.py`, `sim_device.py`, `e2e_conversation.py`, `e2e_live.sh`; 96 unit tests over a fake streaming API.

## Design Decisions
- Eager runner with every call deferred: with `run_tools_eagerly=False` the runner re-ran every tool after `append_messages()` (double HA writes).
- Memory tool on the task model only (it cost Sonnet 1.4 s per turn); a memory index rides in each turn's context note.
- Model switch starts a new runner (per-model tool sets); unpaired server tool uses are not stored (Fable 400).

## Problems & Resolutions
- The bridge kept main's own client after fix-hamcp-session-hygiene; `weixue_hamcp` serves agent-service only.
- Compose 5.3 gives a service recreated alone with an empty `network_mode` no network; recreate with setup.sh or with mcp-bridge.

## Open Questions
- Fable's interim text inside the 6 s hold is spoken ("Now the history."); keep (brief) or end the reply at the switch to the task model?
- Live stack now mounts this worktree's `server/data`: redeploy main with `setup.sh` after merge, before removing the worktree.

## Deferred checks
- Device audio (speech-end → first audio, spoken steer/barge-in): feat-llm-claude-agent gate / audio session.
- Approve path of a real held call and ha-mcp behaviour after a dropped client request (needs Jay's approval test); office/tunnel mode run; `pause_turn` never occurred in 20 tries (runner resume unit-tested only).

## Test Coverage
96 agent-service tests and 760 server tests pass. Live e2e (`docs/logs/feat-agent-service-e2e-2026-10-10.log`): short ask, free light action restored, denied held call, long task delivered WHEN_IDLE, steer then stop, parallel chat, barge-in (11 ms cancel), restart, device disconnect.
