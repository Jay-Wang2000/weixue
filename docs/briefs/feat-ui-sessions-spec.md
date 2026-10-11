# Feature Spec: Session switcher, service side (M2)
Task: feat-ui-sessions | Branch: feat/ui-sessions | Date: 2026-10-11

## Summary
The device's chats list now comes from agent-service's chat index. Protocol v1.2 adds an `agent_chats` payload (current chat, titles, state, age) for devices at feature level 2; `switch_chat` / `new_chat` are receipted after the service answered and followed by a refreshed list. v1.1 devices are unaffected.

## Changes
- Wire (`weixue_agent_msg.py`, `weixue_agent_msg.h`): `agent_chats` ≤ 1024 B, ≤ 12 entries, current never cut; `features.agent: 2` in hello and `X-Weixue-Agent-Feature: 2` on polls (patch 35).
- Gateway: chat list from `GET /v1/chats` (stream connect, after every switch) and the stream's `chats` deliveries; dedup; part of every snapshot; seq-acked like items; placeholder titles fall back to the newest task title; stream also starts on agent polls.
- Switch receipts: `accepted` on 2xx, `stale` otherwise (device shows "Couldn't switch").
- Agent service: a switch closes another chat's spoken-answer window.
- Firmware: store keeps the newest list; UI model takes chats/current from it, task chips still win; host tests.
- `agent_chats_e2e.py`, sim device `--level`, fixture `chats`; docs (protocol v1.2, firmware, agent-service).

## Design Decisions
- Separate kind with level negotiation, not an `agent_sync` field: lists change independently of task snapshots and would not fit next to 32 task ids.
- Receipt waits for the service so a failed switch is visible on the device.

## Problems & Resolutions
- Sim smoke: a new chat's first utterance answered the previous chat's open question (cross-chat 20 s window). Fixed in `ChatIndex.switch`, with a test.

## Open Questions
- None.

## Future Work
- Voice synonyms ("switch to <title>", "back to the last chat") via `resolve` / `back`.

## Test Coverage
Server 879 passed (15 new gateway/wire tests: v1.1 drop, level-2 poll, snapshot, dedup, title fallback, switch accepted/stale, new_chat, stale fetch), agent-service 110 passed, firmware host tests pass. Sim acceptance 38/38 on the branch service (5 chats, 10 switches each followed by a voice turn verified by `turn_latency` and `accepted` trace in chat events, service restart), 33/33 against the deployed service, gateway restart 10/10, v1.1 sim device gets no `agent_chats`. Board: release build flashed; fixture lists applied and adopted (`agent ui: chats=5 current=…`); left on a clean release boot. Deferred: on-board tap switching and rendering of the expanded list (release build has no FACEDUMP or touch injection) and live-gateway delivery until this branch is deployed; the voice-window fix needs the agent-service redeploy.
