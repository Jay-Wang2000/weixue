# Management view and chat list on the 1.75C, live stack (2026-10-11)

Main `1af430f`, `VOICE_LLM=claude_agent`, diag build (`WEIXUE_FACE_DIAG=1`) flashed, then the release build flashed back. Device lock held 12:20:50Z–12:33Z (12 min). No audio played (`diag volume 0`, `diag bedside day`; `earcon=chat_switch` is logged, output volume 0).

Logs: `mgmt-view-check-2026-10-11-serial.log` (diag boot and test, `L<n>` below = line), `mgmt-view-check-2026-10-11-release-serial.log` (release boot), screen dumps `mgmt-view-check-2026-10-11-png/` (`face-NNN-*.png`, decoded from the `FACEDUMP`s).

## Observed

| Step | Result | Evidence |
|---|---|---|
| Boot, level 2 | `agent: protocol v1 level 2`; device applied the gateway's `agent_chats` (n=5, current `…QQ`, header `story about a lighthouse`) and an 8-task snapshot | L108, L261–263 |
| Open management view | `diag swipe left` (docs: left swipe opens, right closes; no right swipe was needed) → `MonitorPanel: page=1`, `page=panel`; page 1 shows pill `story ab… · Chats ∨`, tasks `Yes.` (Question), `Please call ha_cal…` / `story about a lig…` (Done · unread), `+5 more`, `Home · 家` | L1016–1020, `face-003` |
| Expand chat list | `diag tap 233 98` → `diag dump` `"chats":true`; list: `+ New chat`, current `story abou… · Question` (cyan outline), `Please call ha_… · Unread`, `What time i… · 17 min`, `Please call ha_call_serv · 57 min`; scroll arc at 3 o'clock; 5th chat (`New chat`) below the fold | L1530–1636, `face-005`/`face-006` |
| Service comparison | `GET /v1/chats` (agent-service, from inside the container): 5 chats, current `…QQ`. Titles, order, current, ids equal the device's list (gateway log `agent chats queued … chats=5/5 seq=1791720680098`, equal to the device's seq) | below |
| Switch chat | Row 2 tapped (`diag tap 233 285`): `MonitorPanel: page=face`, `agent response: kind=switch_chat chat=…NDZ8 … via=tap seq=1`, `agent receipt: switch_chat seq=1 result=accepted`, `earcon=chat_switch`, then `agent: chats seq=1791721571332 current=…NDZ8 n=5` and `agent ui: chats=5 current=…NDZ8 header=Please call ha_call_serv`. Gateway: `agent switch_chat … service=200 result=accepted`; service `current_chat_id` = `…NDZ8`, `previous_chat_id` = `…QQ`. No voice follow-up needed or sent | L4334–4335, L4349, L4355, L4404–4410 |
| Reopen after switch | List reordered: current `Please call …` (Unread) first, `story about a l… · Question`, `What time i… · 31 min`, `Please call ha_call_serv · 1 h` | `face-013` |
| Restore | Switched back to `story about a lighthouse` (seq=2, `accepted`, `chats current=…QQ`); service `current_chat_id` = `…QQ` again | L8175–8197 |
| Release build | `strings build/xiaozhi.bin \| grep -ci diag` → 0; `face_diag.h` `WEIXUE_FACE_DIAG 0`; reset, `State: activating -> idle` at 4.4 s, `protocol v1 level 2`, `agent: chats … n=5`, `agent ui: chats=5 current=…QQ`, 0 `diag` lines in 275 lines; no panic or reboot loop | release log L107, L190, L263–265 |

Chat ids in the logs are service ids; the device id is not recorded here.

Service chats at the first comparison (`GET /v1/chats`): `story about a lighthouse keeper…` (current, working, unread, open task), `Please call ha_call_service wit…` (unread), `What time is it?` (state `working`, open task), `Please call ha_call_service wit…` (idle), `New chat` (placeholder, idle).

## Defects and mismatches

1. **Age is stale after a board reboot.** The device showed `What time i… · 17 min` and `Please call ha_call_serv · 57 min` after the reboot; the chats' real ages were 28 and 68 min (11 min low: the age in the list resent with the boot snapshot is the one computed at the original send, 12:11Z, and the device counts from receipt). After the switch's fresh list the same rows read `31 min` and `1 h` (correct). `face-006` vs `face-013`. Severity: low.
2. **State chip missing for `What time is it?`.** The service lists it `state=working` with an `open_task_id` (last activity 11:56Z); the device row shows only the age, no `Working` chip, in both lists. Not investigated further (could be the gateway's mapping of that open task, or a task the device does not hold). Severity: low; needs a look at the gateway's chat-state mapping.
3. **Two chats share one truncated title.** `Please call ha_call_service wit…` appears twice (rows 2 and 4; pill `Please c…`); only age and state tell them apart. Titles come from the service's first utterance. Severity: cosmetic.
4. **A `diag dump` taken about 2 s after the expand tap shows the collapsed page** although the dump JSON says `"chats":true` (`face-004`, `face-010`); dumps taken 8 s after (`face-005`, `face-006`, `face-013`) show the expanded list. Same pattern twice; likely the snapshot being taken before the expand re-render, not seen on the real display. Severity: diag tooling only.
5. **Panel timeout vs diag dumps.** Page 1 closes after 30 s without touch; a `FACEDUMP` streams for about 14 s, so a slow script lost the panel once (L3053 `page=face`; the row tap at L3804 hit the face and was repeated at L4323). Not a defect of the UI.

Not exercised: touch scrolling of the list (`diag swipe up/down`) and the 5th chat below the fold, `New chat`, the post-switch face (`Switching…` / `Last: <summary>`; the dump was not taken inside its 6 s window), real-finger taps.
