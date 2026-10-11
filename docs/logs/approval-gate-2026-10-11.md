# M2 approval gate on the live stack and board, 2026-10-11

Main 7408624 deployed (`HA_MCP_READ_TIMEOUT_S` 35, `VOICE_LLM=claude_agent`, `VOICE_ASR=GoogleSTTStream`). Diag build of main flashed, real agent service and ha-mcp. Held call: `ha_call_service` `input_boolean.toggle` on `input_boolean.weixue_session_probe` (no such entity; Home Assistant refuses the replay). Line refs are lines of the logs beside this file.

| Round | Expected | Observed | HA executions | Verdict |
|---|---|---|---|---|
| A approve | `needs_you` earcon, card, tap approve, receipt `accepted`, `approved` earcon, one replay, executed | `earcon=needs_you` L756, card open L757, one tap, receipt `accepted` L1450, `earcon=approved` L1454 (A-serial.log); decision `approve` and one replay in A-agent-service.log L25 and L28 | 1 (`ha call finished`, outcome `failed`: HA refused the missing entity) | pass |
| B deny | receipt `accepted`, state denied, no replay, card closes, chip updates | not run: blocked by the permission classifier (real HA replays) | not run | not run |
| C duplicate taps | one replay, second receipt `duplicate` | not run: blocked by the permission classifier (real HA replays) | not run | not run |
| D expiry | card withdraws, task expired, no replay | not run: blocked by the permission classifier (real HA replays) | not run | not run |

## Round A

Run: `scripts/audio_session.sh --no-flash -- --items boot,earcons --count 1` (board flashed with the diag build beforehand). Table: `approval-gate-2026-10-11-A.md` (boot 1/1, earcons 1/1).

| Step | Time (UTC) | Source |
|---|---|---|
| Task `tsk_01M4NG76ATW0C5239WK83F8M1G` created | 12:50:18 | A-serial.log L544 |
| `ha call pending` (hold, 8007 ms) | 12:50:28.404 | A-agent-service.log L11 |
| `earcon=needs_you`, card open on device | 12:50:29 | A-serial.log L756, L757 |
| `diag card approve` sent | 12:50:42.98 | A-serial.log L1425 |
| Device `agent receipt: approve ... result=accepted` | 12:50:43.26 | A-serial.log L1450 |
| `ha hold decided` approve (service) | 12:50:43.105 | A-agent-service.log L25 |
| `ha call finished` (replay, 115 ms) | 12:50:43.224 | A-agent-service.log L28 |
| `earcon=approved` | 12:50:43.31 | A-serial.log L1454 |

Latency from the tap: about 0.2 s to the receipt and about 0.3 s to the approved earcon (serial timestamps).

Observations:
- The hold stays open past the 15 s the earlier client timeout would have cut: `pending` at 8 s, replay after the tap. The fix of fix-hamcp-client-timeout works on the live stack.
- The driver logged "hold approvable" at 12:50:41, 13 s after the service's `ha call pending` and the card on the device. The delay is in the driver's task polling; not investigated.
- After the failed replay the task went to `needs_input` (device pose `confused`, a notice `ntf_01M4NG811Z09QMCEW463HQFS4Y` queued). Consistent with the agent asking what to do about the refused call.

## Rounds B, C, D

Not run: blocked by the permission classifier (real HA replays). The driver of these rounds is now `scripts/approval_gate.py`; rerun under the device lock with the board on the diag build:

```
.venv/bin/python scripts/approval_gate.py --port <port> --device <device-mac> --rounds B,C,D --out <out-prefix>
```

It logs per-step timestamps, the receipt of each tap, task and trace state at 8 s and 33 s after the tap (B), 10 s and 25 s (C), and at the end of the wait (D). Capture `docker logs agent-service` for the same window and count `ha call finished` for the tasks (expected 1 for C, 0 for B and D).
D waits for the approval TTL (10 min per the lead's brief; not confirmed from the policy).

## Restore

- Release build of main flashed (`approval-gate-2026-10-11-release-flash.log`); `strings` of the image: 0 `DiagConsole` or `diag card` strings. Boot to idle: `StateMachine: State: activating -> idle` at 4.5 s, 0 `DiagConsole` lines (`approval-gate-2026-10-11-release-boot-serial.log`).
- Device lock released.
- Current chat: the board's current chat was `cht_01M4MW1X6BH94BTK44DREH1MN1` before and after round A. During the 40 s release boot capture someone handled the board (pick_up gestures, panel taps, two `new_chat` taps with `via=tap`, a Jarvis wake word at 38 s), and the current chat became `cht_01M4NGJ62D74JRKM2KXEWSVJ8W` ("New chat", n=8 chats). Not changed by these rounds and not reverted. Round A left one extra non-current chat, `cht_01M4NG7606M4ZNPPKATVD5252R`.

## Files

`approval-gate-2026-10-11-A.md`, `-A-serial.log`, `-A-server.log` (xiaozhi-esp32-server), `-A-agent-service.log`, `-release-flash.log`, `-release-boot-serial.log`. MACs redacted; no tokens or ha-mcp URLs in the logs.
