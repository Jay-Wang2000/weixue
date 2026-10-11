# Feature Spec: M2 follow-ups before the milestone E2E
Task: fix-m2-followups | Branch: fix/m2-followups | Date: 2026-10-11

## Summary
The gateway runs each `device_action` once per turn even when a resume replays its event; agent-service speaks a short sentence before a reply's first tool call and streams every delta of the first clause uncoalesced. `agent_scenarios.sh m2` adds the long-task, steer/stop, barge-in and VOICE_LLM rollback rounds against the deployed service.

## Changes
- `claude_agent.py`: per-turn `action_ids`; a replayed `action_id` is logged, not run (the service's 409 stays).
- `prompts/system.md`: pre-tool sentence rule. `clause.py` (copy of the TTS `first_clause_cut`, parity test in the server suite); `TextCoalescer` flushes each delta until the first clause, then coalesces.
- Sim: `agent.config.yaml` rendered by `render_config.py` (VOICE_LLM); own device `aa:bb:cc:dd:ee:02`; `proactive_sim_device.py --replies/--barge-in/--barge-frames/--agent` (played acks).
- Docs updated; log `docs/logs/fix-m2-followups-sim-2026-10-11.log`.

## Design Decisions
- Dedupe scope is the AgentTurn: action events are bound to the turn's `turn_id`; resume and retry stay inside it.

## Problems & Resolutions
- A 70-call task hit the service's 60-iteration cap at 199 s (`max_turns`); LONG is 50 calls (3.3 min). A 5-min task needs more than 60 iterations at ~3.3 s each.
- The feat-ui-sessions sim used the same device id concurrently (its question captured my utterance as an answer); separate id since.

## Open Questions
- A device's 20 s voice window stays bound to another chat's question after `POST /v1/chats` switches chat; close it on switch?
- The system prompt is frozen per epoch: existing chats get the rule only at a new chat or compaction.
- Raise `MAX_ITERATIONS` (60) for long tasks?

## Deferred checks
- Live redeploy of agent-service (prompt, coalescer) and the gateway image: orchestrator, after merge. Measured on the branch service only.
- Audible first audio, spoken barge-in/steer, VOICE_LLM flip on the board: milestone E2E.

## Test Coverage
Server 885 passed (replayed device_action runs once; 20 clause parity cases). Agent-service 112 passed (coalescer first clause en/zh/word cut; pre-tool sentence reaches the stream before the tool runs; first-clause deltas uncoalesced under a 3 s interval). Sim m2 against the deployed service 22/22: task done after 198 s with the device offline, spoken WHEN_IDLE 9 s after the device polled, played ack; steer then cancel; barge-in closed the stream (`gateway_closed`), correction answered; anthropic turn on the legacy path. Branch: "Set bedside mode" spoke first, service_first_text 938/1754 ms (was 2.4–4 s; time-only turn 1.1–1.2 s).
