# Board check: question cards, 2026-10-11 16:16-16:31 UTC

Device lock 16:16:23-16:31:22. Diag build of `fix/question-card-discipline` (firmware identical to main) flashed 16:18; console `fix-question-card-discipline-device-2026-10-11.log` (`L<n>` below); live agent-service (main, not redeployed) excerpt `fix-question-card-discipline-agent-service-2026-10-11.log`; release build of main flashed back 16:30, 0 `diag` strings, idle at 7.3 s (`fix-question-card-discipline-release-boot-2026-10-11.log` L189).

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Live agent-service asks a real choice | `ask_user {"question":"Which temperature should I read?","options":["Living room","Bedroom"]}`; card delivered SILENT, then WHEN_IDLE with speech after the 20 s window | service log 16:24:07-16:24:31; device L1043, L1066 |
| 2 | Card opens on the device from the live service | fail on main: the turn's next progress item (`task:` slot, state `working`) replaced the `needs_input` item 1 s after the WHEN_IDLE card, so no card opened; reproduced with a second request (16:27, device L3138, L3214) | device L1110-1111, L3214-3217; service `delivery queued SILENT agent task:…` 16:24:28.929 and 16:27:30.213 |
| 3 | Card rendering (fixture `clarify` through the live gateway) | card opened, title "Which room?", options Kitchen and Hall as the primary buttons, "Or just say it" footer | L4366-4368, dump L4376-4377, `fix-question-card-discipline-card-2026-10-11.png` |
| 4 | Option tap | `diag card option 2` tapped (314,317); device sent `answer … option=hall via=tap`; receipt `refused` (fixture chat unknown to agent-service, as expected) | L4831-4840 |

Finding 2 is fixed on this branch (progress items are not sent to the device while the task waits on its card; `test_real_choice_makes_one_card_with_option_buttons_and_a_tap_answers_it` covers it). The live end-to-end round (row 1 through a tap answering the task) is deferred until the orchestrator deploys this branch.
