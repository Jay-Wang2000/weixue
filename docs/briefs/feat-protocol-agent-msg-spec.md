# Feature Spec: Server→device agent message (M2)
Task: feat-protocol-agent-msg | Branch: feat/protocol-agent-msg | Date: 2026-10-08

## Summary
Agent wire protocol v1 (contract: `docs/agent-protocol.md`). The server sends `agent` items and `agent_sync` snapshots. The device sends `agent_response` and `played` acks. The firmware has a parser, an 8-task store with seq dedup and a loopback fixture build. The server has a delivery gateway with a fixture source and an agent-service stream source. Built against the fixture (agent-service unfinished).

## Changes
- Firmware: `weixue_agent_msg.h` (parse, limits), `weixue_agent_store.h` (apply/sync/seen/Top/eviction), `weixue_agent.{h,cc}` (main-task apply, RAM ack outbox 1/3/9 s, played complete/cut, loopback). Patch 30: custom-frame routing, `tts` `sentence_id`/`ack`, poll header and body, cut points, `features.agent`. Patch 31: playback-idle hook. `build.sh` adds `WEIXUE_AGENT_LOOPBACK`.
- Server: `weixue_agent_msg.py` (builders, validators, delivery mapping) and `weixue_agent_delivery.py` (mirror, push, poll batches, snapshot, 3 s resend, speech via the proactive queue, session hold until `played`, 30 s `lost`, response routes, fixture routes). Small hooks in `weixue_proactive.py` (rechecked after fix-wave5-server: lease invocation and poll body).
- `agent_msg_fixture.py`, `agent_sim_device.py`, docs `agent-protocol.md` and `proactive.md`.

## Design Decisions
- Upstream hooks are installed from `weixue_agent_delivery.apply()`, so `weixue_patches.py` is unchanged.
- WHEN_IDLE speech uses the proactive announce queue, so the busy→idle gate comes from the existing delivery rules.
- Open cards are never evicted. A new task that arrives while all 8 slots hold open cards is refused: the gateway mirror drops it (ack `dropped`), and the device store returns `full` as a backstop. This is simpler than a `shown` note, which the wire has no field for.

## Problems & Resolutions
- Stalled attempt rebased onto `main` cleanly, finished, simulated run repeated.
- Review fixes:
  - Ids must not contain `#`, `/` or `?` (server and device). The gateway URL-quotes the chat id in agent-service paths and logs a drop for a bad one.
  - `X-Weixue-Agent-Seq` advances only after the main task applied the item: the poll task waits up to 1 s for the apply.
  - The gateway drops `approve`/`deny`/`answer` on a card that is not the task's current card.
  - Once an item is answered, the gateway replays it without `debug_reply`, so the loopback build no longer answers again after a reboot snapshot.

## Test Coverage
Firmware host tests: 205 checks pass. Server pytest: 714 passed, 12 skipped (live/credential tests). Both LOOPBACK and release firmware builds succeed. The simulated run predates the review fixes. Simulated device on the scratch stack (`docs/logs/feat-protocol-agent-msg-sim-2026-10-08.log`): all 9 states applied via poll and via push, all tiers. Loopback approve/answer/ack_seen reached the gateway (`user_ack` recorded). Burst: duplicate re-acked, stale ignored, 2228 B item dropped. A seq-0 boot snapshot rebuilt 7 tasks, and a drop removed 2 tasks. Busy→idle speech started 52 ms after idle. `played` complete and cut (`cut_at_ms` 669) were recorded.

## Deferred checks
- **On-device (board not attached, no `/dev/cu.usbmodem*`):** flash LOOPBACK, then states/approval/clarify/burst on hardware, snapshot after a board reset, `played` acks at volume 0, then flash the release build. These need the board on USB. The live server also needs this branch deployed, since it runs the pre-Wave-5 images.
- **Home audio gate:** spoken WHEN_IDLE/INTERRUPT, 10 s busy→idle on hardware, `cut_off` by wake word and IMU (±250 ms), and gateway restart after unacked speech → `uncertain`.
