# Feature Spec: Codex M2 review fixes — agent service
Task: fix-m2-review-service | Branch: fix/m2-review-service | Date: 2026-10-11

## Summary
Closes F01 F02 F05 F06 F07 F08 F12 and the service side of F09/F10. A hold is owned only through the token our own request returned; unknown execution is a first-class disposition that blocks an identical write; device setters are side effects; mixed and paused server-tool turns follow the API contract; secrets are scrubbed on the data path; notices are delivered; outbox acks are monotonic with the service as the only retry owner.

## Changes
- `ha_adapter.py`: listing never claims a hold (`token_source='response'`); dispositions per tool use (`not_sent`, `unknown`, `blocked`…); unknown blocks an identical write for `wait_seconds` + TTL; atomic single replay (`_claim_replay`). Unowned cards read-only, re-spoken WHEN_IDLE once owned; a token arriving after cancel is denied.
- `turns.py`/`recovery.py`: device rows (`dispatched`, `dispatch_id` from the tool use), repaired as unknown; `notify_user` → `agent` card `ntf_…`, `unread`, tier/TTL.
- `actor.py`/`transcript.py`: mixed turns keep the server call, send client results alone; a settle request runs an unsettled server tool before the next message.
- Scrub: user text, tool results, `store.SCRUBBED` columns, events, trace; monitor text scrubbed, schema 2 accepted.
- `deliveries.py`: `ACK_FROM` transitions, lease+attempt checked, offered speech never reoffered after restart.
- Store: JSON columns (`*_json`, `args_summary`) scrubbed string by string before serialization; PEM pattern stops at END, an unescaped quote or end of string; `Outbox.public` and the trace tolerate a malformed payload row.
- `setup.sh`: agent DB copied to `db-rollback/` with service+monitor stopped before the new image; `rollback()` restores it (attempted DB kept in `db-failed/`).
- Store schema v2 (migration). Docs: agent-service (approval timing, schema-2 deploy), agent-protocol (Service deliveries), brief §8 ownership rewritten to `token_source='response'`.

## Design Decisions
- No ha-mcp request id exists; ownership is token-only (review Q1 recommendation). Device approval now starts after `wait_seconds` (180 s live).
- Notices reuse kind `agent`: no gateway change.

## Problems & Resolutions
- The fake API now rejects server-tool contract violations; tests proved settle is needed.

## Open Questions
- Gateway must stop resubmitting after `deferred` and treat `409` on `accepted` as "do not play" (fix-m2-review-gateway).

## Future Work
- Live redeploy of agent-service and agent-monitor together (schema 2).

## Test Coverage
agent-service 137 passed (plus JSON-column scrubbing, PEM boundary, poisoned delivery row); setup.sh suites 99 passed (DB backup/restore via stub docker); (ownership against a foreign identical call, concurrent decisions, unknown blocking, device cancel/restart, mixed/paused settle, secret scans, ack transitions, notices through the gateway wire builder, v1→v2 migration). agent-monitor 39 passed. Sim (scratch stack): two notices played on the sim device, acks accepted→spoken→played, `shown` ignored in handed_off (`docs/logs/fix-m2-review-service-sim-2026-10-11.log`). Live DB copy migrated read-only. No device/voice run: the milestone E2E covers approvals and audio.
