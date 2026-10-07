# Decisions pending Jay's approval

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

## FYI (proceeding; our own stack, not an external service)
9. Codex Wave 2 code review (`docs/reviews/codex-gpt6-astra-high-wave2-code-review-2026-10-06.md`): fix-first verdict. Three fix tasks queued after Wave 3: `fix-server-review-2`, `fix-turn-isolation`, `fix-firmware-scripts-2`. Includes enabling `server.auth` with the device MAC allow-list and binding OTA/WS to needed interfaces only.

## User-only test items
10. `feat-imu` gesture trials (needs your hands; firmware is merged and flashed): pick-up ×10 (screen relights from dim/off), face-down ×10 (mute glyph + 'Muted', unmute on face-up), double-tap ×10 (stops TTS/notification). Pass ≥9/10 each; record false positives while idle 5 min. Steps in `docs/firmware.md` (IMU section) and `firmware/tools/imu_gesture_test.py`.
8. `feat-voice-integration`: 10× "Jarvis" wake count (2/2 so far).
