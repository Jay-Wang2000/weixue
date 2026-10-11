# fix-input-window-routing board check, 2026-10-11 17:48-17:53 UTC

Build: this branch rebased on main `bf42251`, diag (`WEIXUE_FACE_DIAG=1`, `-diag-flash.log`). Live stack not redeployed (main's agent-service and gateway: an API-raised question goes SILENT, then WHEN_IDLE with speech at +20 s). Device lock 17:48:18-17:53:13. `diag volume 0`, `diag bedside day` first. `L<n>` = line of `-serial.log`.

| Step | Observed | Result |
|---|---|---|
| New non-current chat (`switch: false`), API message (`origin.kind` api): "Tell me the temperature in one of the two rooms I use most, but ask me which one first; the options are Living room and Bedroom." | routed `new_task` in that chat; `ask_user {"question":"Which room?","options":["Living room","Bedroom"]}` (agent-service log 17:50:26) | ok |
| Silent arrival | `needs_input tier=silent` L353, `agent ui: … tier=silent flash=0 earcon=none` L359 | ok |
| Upgrade to WHEN_IDLE | `needs_input … tier=when_idle speaks=1 result=applied` L991 (+20 s); no `agent ui: task=` arrival line, no earcon, no flash | ok |
| Card opens without a tap | `agent ui: card open task=…2P7` L992, 20.1 s after the question; no `diag tap` sent | pass |
| Option 2 | `diag card option 2` L1036, `agent response: kind=answer … option=opt2 via=tap` L1068, `agent receipt: answer … result=accepted` L1077 | pass |
| Task | `ha_search` (read, `executed`, 1 HA call) then `done` (17:50:51); 0 HA writes; nothing left to settle | pass |

Restore: main's release build (provenance matched `bf42251`) flashed (`-release-flash.log`); boot to idle at 4.7 s (`-release-boot` L189), 0 `DiagConsole` and 0 `diag`/`FACEDUMP` lines, 0 `diag` strings in the flashed app image. MACs redacted.
