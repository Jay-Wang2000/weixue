# Decisions pending Jay's approval

**2026-10-07 approvals (Jay: "decision looks good to me")**: items 1–4 confirmed as live; item 5 voice policy diff approved in principle (apply at home, after the drift fix); 5b drift fix approved (file edited locally in ha-agent, apply at home); 13 boundary = option C now, D with Ubuntu; ADR-9 reworked so the timeout lives in our bridge, no `wait_seconds` change (item 5's `wait_seconds` line is dropped). Still to do at home: apply policy, rotate secret path, enable event decisions + PIN in the HA-MCP panel (item 7), run the deferred device tests.


Items here are external-service or user-only decisions (CLAUDE.md). Work continues on everything else. Reply inline or in chat; the orchestrator clears items as they are answered.

## Retroactive confirmations (already live since Wave 2)
1. Google STT: Speech-to-Text v2, model `chirp_3`, location `us`, `language_codes [en-US, cmn-Hans-CN]`, one `["auto"]` retry on error. Alternative: `global` location or v1 `latest_short`.
2. Google TTS: Chirp 3 HD, one default voice per language (en-US, cmn-CN), streaming. Alternative: Neural2 (cheaper).
3. Google credential: existing `ha-voice` service account mounted read-only at `/run/secrets/google-sa.json`. Alternative: a project-scoped service account.
4. Anthropic: `claude-sonnet-5-5` default, `claude-haiku-4-5` fallback, adaptive thinking.

## Open proposals (will be filled by agents / orchestrator)
5. **ha-mcp voice policy diff** — ready for review: `server/policy/voice_policy.proposed.json` (overlay on ha-agent `policy/tool_policy.json`; rationale per rule in `docs/ha-mcp.md`). Frees the two Meross outlets, the curtain, media_player, climate 16–26 °C; `wait_seconds` 180 → 8. Note: the policy is shared with Claude Code, so this also loosens its gates. Apply only after your OK (`server/scripts/render_voice_policy.py` → ha-agent `apply-policy.sh`). Then rotate the secret path (update `~/Projects/ha-agent/.mcp.json` and `server/.env`, recreate `mcp-bridge`).
5a. **Voice tool filter (live now, narrows access only)** — the bridge exposes 7 of ha-mcp's 77 tools to the voice path and refuses `ha_call_service` outside light/switch/cover/media_player/climate (`server/policy/voice_bridge.json`). Confirm or adjust the domain list.
5b. **Policy drift found**: live ha-mcp policy v13 gates `ha_manage_blueprints`, but ha-agent's `policy/tool_policy.json` still names the old `ha_import_blueprint`; running `apply-policy.sh` from that file unchanged would drop the gate. Decide: update the ha-agent file first (recommended).
5c. During the live light test (~21:18 house time) the living-room light was switched off by someone outside the voice path; the agent stopped toggling lights. Future live tests will use a dedicated test entity if you name one.
6. Audio/transcript retention and Google data-logging choice.
7. HA-MCP panel: enable event decisions and set a PIN (needed by feat-approvals).

## Security posture (needs your decision)
13. **Server network/auth boundary** (`fix-server-review-2`, code ready on branch, not merged): upstream `allowed_devices` only skips the token check for a listed Device-Id header, and OTA hands a token to anyone who POSTs; Rancher Desktop on the Mac cannot bind a specific host IP (must use 0.0.0.0). Options: (A) accept both until the Ubuntu move; (B) pin the port forward to the LAN IP via a Rancher/lima override; (C) strict allow-list in `weixue_patches` (OTA gives no token to unlisted ids, WS refuses them); (D) real device credential or TLS/WireGuard. **Recommendation: C now, D with the Ubuntu move.** The branch also fixes the Anthropic tool-index bug, TTS cancel, 5000-byte TTS split, direct_answer decoding, STT fallback, atomic setup.sh with rollback, and a pinned requirements lock (393 tests pass). Merge + live end-to-end wait for home (needs device audio).

## Anthropic client settings (needs your decision)
14. **Anthropic HTTP timeout and retries** — decided 2026-10-07 (Jay): keep the SDK retry; handle network/external-service exceptions gracefully instead (cue or final state to the device within the ADR-11 deadline, nothing stale in history, retries logged). Timeout values unchanged. Ships with `fix-turn-isolation`.

## Deferred to home (device audio needed; HA is reachable from the office via the WireGuard tunnel since 2026-10-07)
- `fix-wave3-firmware` (one-breath wake capture, mute transport, IMU torn-read fix, thinking state, panel send, clock resync): code complete on branch, host tests pass; needs the 4×10 wake trials and mute/panel/TZ checks on the home LAN.
- `fix-server-review-2`: only the barge-in / TTS-cancel audio check; the HA round and auth check gate now (tunnel).

## FYI (proceeding; our own stack, not an external service)
9. Codex Wave 2 code review (`docs/reviews/codex-gpt6-astra-high-wave2-code-review-2026-10-06.md`): fix-first verdict. Three fix tasks queued after Wave 3: `fix-server-review-2`, `fix-turn-isolation`, `fix-firmware-scripts-2`. Includes enabling `server.auth` with the device MAC allow-list and binding OTA/WS to needed interfaces only.

## Current network mode (2026-10-07)
- Office day: Mac + device on Jay's phone hotspot (SSID in `.env`, Mac IP 172.20.10.2); the device gets Wi-Fi + `ota_url` override via USB provisioning. HA is reached through the WireGuard sidecar (`server/.env` WG_TUNNEL=1, profile `tunnel`, task `tool-wg-tunnel`); the policy apply script runs from a container in the tunnel namespace. At home: LAN_IP back to 192.168.8.220, WG_TUNNEL unset, clear the device `ota_url` override.

## User-only test items

**2026-10-07 hands-on results** (`docs/logs/handson-2026-10-07.md`): panel swipe/tap pass; Dublin time pass; bedside by voice + touch exit/return pass; one-breath wake fails as expected; wake count 2 of 10 (attempts not countable from logs); pick-up fired 21× but never from a dimmed screen; face-down and double-tap both reacted per Jay (fewer than 10 tries, done outside the serial capture window, so 0 in the logs); dim→relight and "day mode" not in the logs. New defects filed into fix-wave3-firmware (wake-word fragment reaches STT; probable TTS self-echo barge-in), fix-wave3-server (echo guard) and fix-turn-isolation (57 s stall: Anthropic first-byte timeout + silent SDK retry, see stall RCA).
11. `feat-ui-panel`: real finger swipe-left/right on the CST9217 and a tap on the fixture Approve/Deny card (all gate tests used a virtual pointer). Expect: panel slides in 220 ms, values live, tap logs `notifications/weixue/panel` in `docker logs xiaozhi-esp32-server`.
12. `feat-ui-ring-bedside`: at night (or after saying "set bedside mode"), touch the panel and pick the board up: both should relight into the bedside page.
10. `feat-imu` gesture trials (needs your hands; firmware is merged and flashed): pick-up ×10 (screen relights from dim/off), face-down ×10 (mute glyph + 'Muted', unmute on face-up), double-tap ×10 (stops TTS/notification). Pass ≥9/10 each; record false positives while idle 5 min. Steps in `docs/firmware.md` (IMU section) and `firmware/tools/imu_gesture_test.py`.
8. `feat-voice-integration`: 10× "Jarvis" wake count (2/2 so far).
