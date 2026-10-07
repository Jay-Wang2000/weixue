# Feature Spec: Server fixes from Codex Wave 2 review
Task: fix-server-review-2 | Branch: fix/server-review-2 | Date: 2026-10-07

## Summary
Fixes Codex Wave 2 C1, C3-C6, C8, patch 4 (Jay's option C, strict allow-list) and patch 11, plus a `setup.sh` pre-flight and guarded rollback after gate 1's deploy left the stack down. Host tests pass (461). **Not done:** the live deploy, option C OTA/WS probes, device reconnect with the allow-list and the HA free-light round are deferred to an orchestrator-run live step (live deploy deferred: corporate VPN / office mode); the task completes only when that step passes. Barge-in audio stays deferred to feat-voice-integration.

## Changes
- C1: Anthropic tool indexes allocated on emission; `direct_answer` keeps its index.
- C3: Google TTS cancellable before the first response; abort, newer turn or `close()` cancels the RPC.
- C4: 5,000-byte UTF-8 request split; bounded English sentence segmentation.
- C5: `direct_answer` decoded as a JSON string, framing-aware.
- C6: `["auto"]` retry only on a language-list rejection; bounded receive buffer; bilingual `too_long_reply`.
- C8: staged 0600 render, build, in-image validation, atomic publish, readiness check, rollback of config, images and `.env` (from `.env.deployed`, written after each successful run). A failed `up` takes the rollback; a failed rollback `up` prints a manual-restore block.
- Pre-flight before any docker call: every `REQUIRED_KEYS` entry set; `BIND_IP` `0.0.0.0`/`127.0.0.1` (Linux also a local interface address).
- Patch 11: overlay lock, `--no-deps` + `pip check`.
- Patch 4: `ALLOWED_DEVICES` → `allowed_devices`; unlisted OTA 403, unlisted WS refused whatever its token; ports on `BIND_IP`.

## Design Decisions
- Regressions run upstream `chat()`, OTA and WS handlers from the vendored source.
- Unlisted OTA gets 403, not a token-less 200 that would overwrite the device's stored websocket settings.

## Problems & Resolutions
- Gate 1: binding the Mac's LAN address failed and the unguarded `up` exited with the stack down. Resolved by the guarded rollback and the macOS `BIND_IP` rule.

## Open Questions
- Live `server/.env` needs `BIND_IP=0.0.0.0` and `ALLOWED_DEVICES=7c:0c:5f:42:4b:fc`; the first run has no `.env.deployed`, so its rollback cannot restore `.env`.
- The allow-list is a claimed header; option D (device credential/TLS) comes with the Ubuntu move.

## Future Work
- Live `test_direct_answer_chinese` is flaky.

## Test Coverage
New host tests cover the C1/C5 index and decoding cases through upstream `chat()`, TTS cancellation against a real in-process gRPC server, STT error and buffer handling, and the allow-list through upstream `OTAHandler`/`WebSocketServer`. `setup.sh` runs against a fake docker for success, render/build/validation failure, readiness and `up` failures, failed rollback (manual-restore block, tunnel profile), `.env` restore and tunnel-mode rollback. The pre-flight matrix covers missing keys and `BIND_IP` per system (Darwin rejects interface addresses; Linux accepts primary, alias and peer-local addresses) with no docker call. No live stack, device or HA round ran at this gate; those run in the orchestrator's live step.
