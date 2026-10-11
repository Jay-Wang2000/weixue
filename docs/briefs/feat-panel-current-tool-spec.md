# Feature Spec: Panel shows the current tool
Task: feat-panel-current-tool | Branch: feat/panel-current-tool | Date: 2026-10-11

## Summary
While a task works, the panel task row and the small-face complication show what the agent is doing now (Searching… / Checking the house… / Acting… / Finding a tool… / Thinking…, or the Chinese labels for zh turns), derived from the current tool (decision 68).

## Changes
- agent-service: `activity.py` mapping; `progress.activity` set at turn start and on each tool use, at most one change per second per task (latest wins), only while the task is `progress`, cleared at turn end; a reply ending mid-turn pushes the current progress. Schema: optional `activity` (≤ 24) in `Progress`.
- Gateway: wire v1.3, `progress.activity` (≤ 24 chars); `progress` may carry only the activity.
- Firmware: parser, `ActivityOf`/`ActivityText`, state lines on row and complication; CJK font as LVGL fallback of those labels; `agent ui: activity=… task=…`. Chat chip unchanged.
- Docs: agent-protocol, agent-service, firmware, DESIGN M2 row.

## Design Decisions
- New `activity` field: `phase` is an internal enum, not a label.
- Code execution maps to Searching (it only runs inside the web tools; live turns start with it).

## Test Coverage
Service 200 passed (23 new), gateway/server 1040 passed, firmware host 0 failures (parser, rows, complication, CJK fallback, truncation), host renders, sim round (fake tools: labels in order, items ≤ 1024 B). Board under lock: branch diag build against a scratch stack of this branch (LAN ports, live web search, new chat with switch=false): `activity=Searching…` on device, complication and panel dumps; main release flashed back, idle, 0 diag strings.

## Deferred checks
- Live-stack check after deploy (the live stack runs main; no redeploy).
- Chinese label and voice-turn path on the board (host tests only).
