# Feature Spec: Question cards only for real choices
Task: fix-question-card-discipline | Branch: fix/question-card-discipline | Date: 2026-10-11

## Summary
A question card now exists only for a genuine choice (decision 67): one short question plus 2-4 options the user picks. Open-ended endings ("anything else?") are speech; the turn ends and the task is done.

## Changes
- `awaiting_answer` tool, its prompt line and the "final text ends in ?" text-question path removed; `Interactions._new` refuses an input interaction without 2-4 options.
- `ask_user` validation (`check_question`): question ≤ 120 chars, not open-ended (`routing.OPEN_ENDED`, en + zh); 2-4 distinct non-empty options. A rejection returns "Not asked: <why> … answer directly … end the turn" and creates nothing.
- Prompt `# Questions` rewritten with a good and a bad example; a spoken closing question needs no tool.
- Start-up migration: open option-less questions cancelled (`option-less question retired`), tasks done and read, one log line.
- Docs: agent-service.md, agent-protocol.md, DESIGN.md M2 UI row.

## Design Decisions
- Option labels are limited to display width 12 (4 CJK), the wire's label rule, instead of the 24 chars in the task text: the gateway truncates longer labels.
- A message to a task waiting on its question answers it in the user's own words after the voice window too (before, it left the task stuck in progress).

## Problems & Resolutions
- Board check on live main: the question card never opened. The turn's next progress item replaced `needs_input` on the device 1 s after the card arrived. Fixed: no device progress while a task waits on a card (`needs_input`/`needs_approval`); covered by a test.

## Open Questions
- None.

## Test Coverage
agent-service 177 passed (validation, rejection, guard, real-choice card and tap, late voice answer, migration, no progress over a card); gateway 1033 passed; gateway sim 23/23, including an open-ended ask (no card) and a real choice answered by tap. Board: fixture card shows the question as title and option buttons; `diag card option 2` sent the answer; release flashed back, 0 diag strings, idle. Deferred: the live end-to-end option round (live question → card → tap answers the task) until this branch is deployed; firmware host tests not run (no firmware change).
