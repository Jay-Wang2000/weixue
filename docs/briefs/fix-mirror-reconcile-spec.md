# Feature Spec: fix-mirror-reconcile
Task: fix-mirror-reconcile | Branch: fix/mirror-reconcile | Date: 2026-10-11

## Summary
Finding 76's root cause, taken from the live DB (read only): the task was cancelled at 16:13:49 and that state was delivered (gseq 580). At 16:16:06 the gateway acked `lost` on the handed-off approval speech. The outbox's playback card update then queued a new `needs_approval` copy (gseq 582, int slot). The mirror kept the higher gseq, and the snapshot (latest per slot) restored that card at every boot. Two fixes: the service stops making such cards, and the gateway reconciles.

## Changes
- agent-service `deliveries.py`: no playback card update for an open card whose task ended or whose interaction closed; the snapshot leaves these cards out.
- Gateway: an ended task is never reopened by a later offer or snapshot card (acked `dropped`).
- Gateway reconcile: checks open cards with `GET /v1/tasks/{id}` after the restore, on each stream reconnect, and before every seq-0 poll and session snapshot (the snapshot waits). An ended task gets its terminal item, a 404 task is dropped, and a running task whose card closed becomes `working`. Unsent offers are acked `dropped` and the chat list is refreshed. It logs `mirror reconcile dropped task=… reason=…`, with a 2 s limit per request. If the service is unreachable the mirror is unchanged and this is logged once.
- Docs: agent-protocol.md (Reconcile, Restart, Snapshot) and server.md (sim round).

## Design Decisions
- The terminal item copies the service's own terminal card (`seen:false`).

## Open Questions
- Terminal items never carry `seen:true`. After a reboot a cancelled or failed task shows again as unseen until tapped, for up to 24 h. This was already the case before this change.
- Handed-off WHEN_IDLE approval speech is still played after its interaction closes (16:15:38 `spoken`, two minutes after the approval).

## Test Coverage
There are 7 new gateway unit tests (cancelled→terminal, 404→drop, unreachable→unchanged and logged once, the boot and session snapshots wait, a terminal offer without an ack is applied and the incident's re-card is refused, the restore never reopens) and 2 outbox tests. The recovery sim's new `reconcile` round passes (`docs/logs/fix-mirror-reconcile-sim-2026-10-11.log`). Suites: server 1049 passed, agent-service 224 passed.

Deferred: board check (the orchestrator deploys main, reboots, and confirms the card is gone). The board was not touched.
