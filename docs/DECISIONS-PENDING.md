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

## Incident 2026-10-07 (office)
- The fix-server-review-2 gate deploy stopped the live xiaozhi server (port bind on LAN_IP fails under Rancher; branch setup.sh had no rollback on a failed `compose up`). Restore = `cd ~/Projects/weixue/server && scripts/setup.sh` on main (rebuilds main images, keeps the tunnel). Live `.env` now also carries `ALLOWED_DEVICES` and `BIND_IP=0.0.0.0` for the branch (0.0.0.0 equals today's exposure: main already publishes on all interfaces). FYI, no decision needed.

## M2 agent service — decisions before implementation (brief: `docs/briefs/feat-agent-service.md`, 2026-10-07)
15. **Anthropic model config**: `claude-fable-5-1` with `fallback_model=claude-opus-5-5`; effort low for chat sessions, high for task sessions; runaway guards only (max_turns 12/60, max_budget_usd 5/25). Note: Fable 5.1 requires 30-day data retention enabled on the API org, otherwise every call fails with 400 — check your personal org setting.
16. **Anthropic client behaviour / key source**: keep the CLI's own retry and timeout defaults with retries visible in logs; disable non-essential traffic; single key source `server/.env` (ADR-10 wording to match).
17. **Networking**: agent-service stays on the Docker project network, published on 127.0.0.1:8010 with a bearer token; HA traffic goes through an authenticated relay added to mcp-bridge (already in the tunnel namespace). Alternative: agent-service joins the wg namespace (tunnel toggles and wg restarts would kill running tasks). Recommendation: the relay.
18. **HA approval decision route (changes ADR-9 wording)**: A = HA event bus `ha_mcp_approval_response` (needs a dedicated HA admin long-lived token, a PIN on the bus, event decisions enabled, lockout handling). B = HTTP `/api/policy/approve|deny` over the existing secret path plus polling `/api/policy/pending` every 2 s while anything is pending. Recommendation: B for v1; ADR-9 becomes "HTTP decisions, event bus optional for push". Also: deny at boot any ha-mcp pending tokens matching our interrupted tasks (recommend yes); allow the identical re-call after a post-wait approval (recommend yes).
19. **HA tool surface for the agent path** (`server/policy/agent_bridge.json`): the current 7 voice tools plus read-only automation/config read tools, so the sunset-automation acceptance drafts without applying; automation/config writes stay hidden until you apply the policy overlay.
20. **Gate model + voice approval**: single decision — HA tools pre-allowed inside the SDK, ha-mcp policy decides held vs free, the device panel approves; `can_use_tool` only guards non-HA tools. Voice may deny or cancel but never approves in v1 (the room, the TV or the device's own TTS could say "yes").
21. **Memory + retention**: SDK auto-memory on, one project memory dir shared across chats; transcripts kept until a chat is deleted; task events and logs pruned after 30 days; no audio stored.
22. **Scope**: no web tools in v1 (weather via the HA weather entity); the dev-job dispatcher becomes a separate task `feat-dev-dispatch`; feat-llm-claude-agent acceptance reworded to "a 5-min voiceless task survives a device disconnect and its result arrives via WHEN_IDLE".

## Agent UI design review (artifact https://claude.ai/artifact/ExW3FbgpovZD3zk6LncHfv · report `~/Projects/research/reports/weixue-agent-ui-v1.md`, 2026-10-07)
23. **Approve the design direction** (glanceable character: eyes stay, one glyph + one line per state, amber ears = needs you, approve-left/deny-right, numbered clarification options, tasks page on swipe-up, chats page). Deviations from backend §5 are listed in report §9 (full ring stays voice-only; countdown as footer text; task list not on swipe-left).
24. 1. **批准超时与夜间策略**：设备只显示服务端 `timeout_s`，不自定时限；后端 §5 的倒计时取 ha-mcp 策略的 5 min（ADR-9：到期时间由我们的应用记录，不改 `wait_seconds`）。默认 300 s 与后端 §5 一致，还是更短的 120 s？超时自动拒绝；说话时暂停累计上限 30 s。床头窗口内的 WHEN_IDLE 批准：延长 `timeout_s` 到 07:30，还是按原时限自动拒绝并在早上告知？（建议：延长到 07:30，INTERRUPT 不受影响。）
25. 2. **语音批准严格度**：只接受显式动词（approve / do it / 批准 / 同意），裸 "yes" 触发复述；irreversible / spends money 只能点按。接受否？
26. 3. **D1 字体**：随固件带 `common_20_4`（量 bin 体积，占 assets 分区），还是接受英文单语的二级标签（Approve / Deny 按钮上无 批准 / 拒绝）？（建议：先量体积，<1.5 MB 则带。）
27. 4. **任务列表手势** — decided 2026-10-07 (Jay): the existing horizontal-swipe view becomes a single management view (current chat + chats switcher, agent tasks, HA status/approvals); chats are selected inside it; no swipe-up list. Design being amended; DESIGN.md updates with feat-ui-agent-states/feat-ui-sessions.
28. 5. **Approve 左 / Deny 右**（现有面板约定）而非手表约定的「确认在右」。保持左 / 右？

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
