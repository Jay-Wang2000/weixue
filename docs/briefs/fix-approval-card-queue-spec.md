# Feature Spec: approval cards preempt an open question card
Task: fix-approval-card-queue | Branch: fix/approval-card-queue | Date: 2026-10-11

## Summary
Decision 66 = A: an approval card that arrives while a question card is open takes its place. The question returns once no approval is waiting. The gateway never sends an approval `silent`, and the boot snapshot puts approvals first.

## Changes
- Gateway: a `SILENT` approval goes out `when_idle`. The boot snapshot orders approvals, then questions, then the rest.
- Device: preemption follows each card's tier timing. It never happens under a finger, during an answer in flight or `Not sent`, or for an approval that was waiting before the question opened. Cards of the same kind advance after 20 s untouched. At boot the newest approval opens first. Logs `card preempted|advanced task=… by=…`.
- Driver `scripts/approval_gate.py`: rounds A, B, C, D, P. It cancels only in probe chats and stops when a message is routed into another chat.

## Pre-step
`tsk_01M4NG76…` and `tsk_01M4M7YC…` were cancelled via the API at 15:07 UTC and confirmed `cancelled` before the run.

## Board run (diag 6620d7a, same firmware as HEAD)
A, B, C and P passed, with replays 1, 0, 1 and 1. In P, a face tap opened the question, the approval preempted it, and the question returned after the tap. Release restored: 0 diag strings, idle 5.8 s; lock 16:00–16:16 UTC. See `docs/logs/approval-card-queue-2026-10-11-board.md`.

## Problems & Resolutions
- Someone at home was talking to the device. An early probe message was taken as the answer to their question in chat "Jarvis. Jarvis.", and that driver cancelled the task there (`tsk_01M4NSX0…`). This is now guarded against.
- After B's deny, ha-mcp denied the identical call at once. A and P were re-run on the also non-existent `…_probe_p`.

## Open Questions
- agent-service: an API `ask_user` card arrives `silent`. After the 20 s voice window, a `working` item replaces it, and the window captures the next message.

## Test Coverage
Passing: 129 gateway tests, device `TestPreempt`/`TestCardAdvance`/`TestBootOrder`, 16 driver tests. Live against the old gateway: preemption, return, cleanup. Unit-tested only: tier floor, boot ranking, 20 s advance. Deferred: board round D, plus boot ordering and the tier floor after the main deploy.
