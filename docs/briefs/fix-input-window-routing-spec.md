# Feature Spec: Answer routing and question-card delivery
Task: fix-input-window-routing | Branch: fix/input-window-routing | Date: 2026-10-11

## Summary
Only the device's voice follow-up or the answer API can answer an open question. Question cards never arrive silent, and the device opens a card that is upgraded from silent without a tap (decision 70, question-round defect 1).

## Changes
- agent-service: the voice window and answer-by-message apply only to `origin.kind` voice/sim. An API message goes to the chat it names.
- Matching rule `routing.answers_question`: the reply names an option or a position within 10 words (15 CJK characters), or it is a short answer of at most 3 words (6 CJK characters) that is not a stop phrase.
- A message that is not the answer is a new turn in its chat. The open question is cancelled, its card is withdrawn and the model gets a note.
- A question spoken in a non-voice reply is carded WHEN_IDLE with its speech straight away, and it is not repeated at +20 s.
- Gateway: SILENT `needs_input` becomes `when_idle`.
- Firmware `SetTasks`: an open card upgraded silent → when_idle/interrupt is queued for auto-open, with no Arrival (no earcon, no flash).

## Design Decisions
- An unrelated message continues the chat's open task; there is no second task.
- Approval speech is unchanged, so the `needs_you` earcon stays.

## Test Coverage
- agent-service: 222 passed (routing matrix).
- Gateway: 1041 passed.
- Firmware host tests pass; `TestTierUpgrade` fails without the fix, and the preemption tests are intact.
- Sim: 28/28 plus the activity round.
- Board on the live main stack: the card auto-opened after 20.1 s without a tap. Option 2 was accepted and the task finished done. Release build restored, 0 diag strings, idle.

## Deferred
- Live check of the service and gateway changes (API question carded when_idle at once; API probe during a household window) until this branch is deployed.
