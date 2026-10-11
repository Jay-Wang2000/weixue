# Feature Spec: xiaozhi provider claude_agent (M2)
Task: feat-llm-claude-agent | Branch: feat/llm-claude-agent | Date: 2026-10-11

## Summary
`VOICE_LLM=claude_agent` makes every voice turn one message to agent-service; its SSE reply streams into Google TTS with a first-clause cut. Default stays `anthropic` (live untouched); both paths stay selectable.

## Changes
- `overlay/core/providers/llm/claude_agent/claude_agent.py`: SSE turn mapping, heartbeats yield `""`, reply_end of any reason ends the turn, ≤1 s switch-race wait, retry/resume/fallback/breaker, `turn_latency` line.
- Barge-in: `count_aborts`, a newer `chat()` and session end close the stream (<100 ms in tests); no `/interrupt`.
- `device_action`: service app tool → SSE event → gateway runs it on device MCP (ADR-13 list on both sides, gateway reuses `weixue_device_tools`) → result POST.
- `google_tts.py` `first_clause` (default on); `render_config.py` VOICE_LLM; `validate_config.py` nointent check.
- Sim: `agent_scenarios.sh live|branch`, sim device `--mcp-tools`.

## Design Decisions
- SSE read runs as aiohttp on the connection loop (not sync httpx), so a cancel from the abort hook closes the socket immediately.
- Fallback lines go straight to the turn's TTS sentence, never into history.

## Problems & Resolutions
- TextCoalescer already flushed the first delta on main; added a service-level test.

## Open Questions
- First audio still waits for the first clause to complete; the coalescer's 150 ms after the first delta delays that (sim total_ms 1.2–1.4 s with tone TTS).
- "Set bedside mode": the model called device_action before speaking (service_first_text_ms 2.4–4 s); a pre-tool sentence rule may be wanted.

## Future Work
- Cleanup task deleting the anthropic path after the M2 E2E; orchestrator flips VOICE_LLM after the gate.

## Test Coverage
Server 864 passed (40 provider tests with a fake SSE service: reply_end reasons, heartbeats, abort, stale turn, retry, 4xx/503, stall→resume, lost/fallback, breaker, switch wait, device_action whitelist, metrics, wake-cue fence; first-clause en/zh/mixed; VOICE_LLM render). Agent-service 109 passed (6 device_action/coalescer). Sim: live 5/5, branch 11/11 (`docs/logs/feat-llm-claude-agent-sim-2026-10-11.log`).

Deferred to the milestone E2E (audio/device): audible first-audio latency with real Google STT/TTS; spoken barge-in and steer on the board; wake cue interplay; real `self.screen.set_bedside` on the 1.75C; HA-read `answer_audio_ms`; live VOICE_LLM flip and rollback to anthropic.
