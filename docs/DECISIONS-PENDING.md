# Decisions pending Jay's approval

Items here are external-service or user-only decisions (CLAUDE.md). Work continues on everything else. Reply inline or in chat; the orchestrator clears items as they are answered.

## Retroactive confirmations (already live since Wave 2)
1. Google STT: Speech-to-Text v2, model `chirp_3`, location `us`, `language_codes [en-US, cmn-Hans-CN]`, one `["auto"]` retry on error. Alternative: `global` location or v1 `latest_short`.
2. Google TTS: Chirp 3 HD, one default voice per language (en-US, cmn-CN), streaming. Alternative: Neural2 (cheaper).
3. Google credential: existing `ha-voice` service account mounted read-only at `/run/secrets/google-sa.json`. Alternative: a project-scoped service account.
4. Anthropic: `claude-sonnet-5-5` default, `claude-haiku-4-5` fallback, adaptive thinking.

## Open proposals (will be filled by agents / orchestrator)
5. ha-mcp voice policy diff (`server/policy/voice_policy.proposed.json` once feat-ha-mcp lands) and secret-path rotation (also updates `~/Projects/ha-agent/.mcp.json` and `server/.env`).
6. Audio/transcript retention and Google data-logging choice.
7. HA-MCP panel: enable event decisions and set a PIN (needed by feat-approvals).

## User-only test items
8. `feat-voice-integration`: 10× "Jarvis" wake count (2/2 so far).
