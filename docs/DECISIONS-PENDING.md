# Decisions pending Jay's approval

**2026-10-07 approvals (Jay: "decision looks good to me")**: items 1–4 confirmed as live; item 5 voice policy diff approved in principle (apply at home, after the drift fix); 5b drift fix approved (file edited locally in ha-agent, apply at home); 13 boundary = option C now, D with Ubuntu; ADR-9 reworked so the timeout lives in our bridge, no `wait_seconds` change (item 5's `wait_seconds` line is dropped). Still to do at home: apply policy, rotate secret path, enable event decisions + PIN in the HA-MCP panel (item 7), run the deferred device tests.


Items here are external-service or user-only decisions (CLAUDE.md). Work continues on everything else. Reply inline or in chat; the orchestrator clears items as they are answered.

## Retroactive confirmations (already live since Wave 2)
1. Google STT: Speech-to-Text v2, model `chirp_3`, location `us`, `language_codes [en-US, cmn-Hans-CN]`, one `["auto"]` retry on error. Alternative: `global` location or v1 `latest_short`.
2. Google TTS: Chirp 3 HD, one default voice per language (en-US, cmn-CN), streaming. Alternative: Neural2 (cheaper).
3. Google credential: existing `ha-voice` service account mounted read-only at `/run/secrets/google-sa.json`. Alternative: a project-scoped service account.
4. Anthropic: `claude-sonnet-5-5` default, `claude-haiku-4-5` fallback, adaptive thinking.

## Open proposals (will be filled by agents / orchestrator)
5. **ha-mcp voice policy diff** — approved in principle 2026-10-07; overlay no longer touches `wait_seconds` (ADR-9 revised); apply at home after the drift fix (5b), then `render_voice_policy.py` + `apply-policy.sh`. Becomes moot for the agent path once M2 exposes all tools under the ha-mcp policy (decision 19).
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
15. **Anthropic model config** — accepted 2026-10-07 (Jay): `claude-fable-5-1`, fallback `claude-opus-5-5`; effort low for chat, high for tasks; runaway guards only. Jay to check 30-day retention on the personal API org.
    - Note (2026-10-07, Wave 5 review reconciliation): the retention confirmation is still open as brief §14 Q7; S0 needs it.
16. **Anthropic client behaviour / key source** — accepted 2026-10-07: SDK retry/timeout defaults, retries visible in logs, single key source `server/.env`.
17. **Networking** — decided 2026-10-07 (Jay): the simple solution, no relay. agent-service calls ha-mcp directly via HA_MCP_URL; in office mode it joins the wg namespace exactly like mcp-bridge (network_mode service:wg when WG_TUNNEL=1). The backend migrates to the home Ubuntu server (LAN, reached over Tailscale), where no tunnel is needed.
    - Note (2026-10-07, Wave 5 review reconciliation): the ADR-9 8 s pending boundary is an in-process adapter inside agent-service calling ha-mcp over HTTP directly (review Q4, orchestrator).
18. **HA approval decision route** — decided 2026-10-07 (Jay): B. Device decisions go to ha-mcp `/api/policy/approve|deny` over the existing secret path; while anything is pending our service polls `/api/policy/pending` every 2 s. Event bus optional later for push. ADR-9 wording updated.
    - Note (2026-10-07, Wave 5 review reconciliation): owner of this contract on the agent path is feat-agent-service; mcp-bridge keeps its current behaviour on the legacy voice path until feat-llm-claude-agent. At startup only pending calls whose ownership is provable (our correlation record) are denied; others are left and shown on the monitor (review Q5, orchestrator).
19. **HA tool surface for the agent path** — decided 2026-10-07 (Jay): no extra filtering; the agent sees every ha-mcp tool; the security decision lives in one place, the ha-mcp policy (held/free). The xiaozhi voice filter becomes moot once the LLM path moves to the agent service.
20. **Gate model + voice approval** — accepted 2026-10-07: single decision (ha-mcp policy decides, device panel approves, SDK gate only for non-HA tools); voice can deny/cancel, never approve in v1.
    - Note (2026-10-07, Wave 5 review reconciliation): "never approve" conflicts with item 25's explicit phrases; open as brief §14 Q1. The build is deny-only until answered.
21. **Memory + retention** — accepted 2026-10-07.
22. **Scope** — decided 2026-10-07 (Jay): web tools ARE in v1 (SDK WebSearch/WebFetch enabled for the agent; weather and lookups via web allowed); dev-job dispatcher stays a separate later task; the 5-min voiceless acceptance reworded as recommended.

## Agent UI design review (artifact https://claude.ai/artifact/ExW3FbgpovZD3zk6LncHfv · report `~/Projects/research/reports/weixue-agent-ui-v1.md`, 2026-10-07)
23. **Design direction** — approved 2026-10-07 (Jay, with the management-view amendment).  (glanceable character: eyes stay, one glyph + one line per state, amber ears = needs you, approve-left/deny-right, numbered clarification options, tasks page on swipe-up, chats page). Deviations from backend §5 are listed in report §9 (full ring stays voice-only; countdown as footer text; task list not on swipe-left).
24. **批准超时与夜间策略** — decided 2026-10-07 (Jay): 300 s (server `timeout_s`); bedside WHEN_IDLE approvals extend to 07:30 as recommended.
    - Note (2026-10-07, Wave 5 review reconciliation): the ha-mcp token can expire before 07:30 and a device card cannot extend it; expired-card behaviour is open as brief §14 Q3.
25. **语音批准严格度** — decided 2026-10-07 (Jay): explicit phrases only; bare "yes" gets a repeat-back; irreversible / money actions tap-only.
26. **D1 字体** — decided 2026-10-07 (Jay): measure the font binary first; ship if small (<1.5 MB), else English-only sublabels.
27. 4. **任务列表手势** — decided 2026-10-07 (Jay): the existing horizontal-swipe view becomes a single management view (current chat + chats switcher, agent tasks, HA status/approvals); chats are selected inside it; no swipe-up list. Design being amended; DESIGN.md updates with feat-ui-agent-states/feat-ui-sessions.
28. **Approve 左 / Deny 右** — decided 2026-10-07 (Jay): keep.

30. **Monitor board exposure** — decided 2026-10-07 (Jay): bind 127.0.0.1 only. The backend moves to the home Ubuntu server on the LAN, reached over Tailscale; the board is then opened through the tailnet (no public exposure, no basic auth needed for v1).

## M2 open questions — answers 2026-10-07 (Jay)
31. **Q1 voice approval**: explicit phrases (approve / do it / 批准 / 同意) approve REVERSIBLE actions only; irreversible or money actions are tap-only; a spoken decision applies to the single card on screen; bare "yes" gets a repeat-back. Supersedes the "voice never approves" line of decision 20.
32. **Q3 overnight card vs HA token**: no special case. The card expires with the ha-mcp token; the agent acknowledges the expiry (tells the user it lapsed, offers nothing automatic).
33. **Q7 retention**: unknown; go with defaults. S0 spike verifies a Fable call with the personal key; on a retention 400 the service falls back to Opus and the monitor shows it; Jay checks the org setting later.
34. **Q8 test entity**: `light.living_room_ceiling_lights` may be toggled in live tests (restore its previous state afterwards). Home audio window: still to be named.
35. **Q2 90 s deadline** — decided 2026-10-07 (Jay): the agent keeps working as a background task; the 90 s cap only forces a spoken final state when no speech has happened (device channel timeout ~120 s). **Q6 delivery after restart** — recommendation recorded (Jay to object if not): add a device playback ack (`played`/`cut off`) and a user ack (tap/ask clears unread) to feat-protocol-agent-msg; after a restart with no ack recorded, never repeat speech, mark the message unread with "may not have played".

36. **Conversation model for long tasks** — decided 2026-10-07 (Jay): NO separate 90 s deadline on top of the normal 8 s no-voice window. Flow: wake → "do X" → the agent acknowledges and starts → after 8 s without voice the exchange ends normally → the work continues in the SAME Claude session (the chat's in-flight turn is the task, no forked task sessions) → the result is reported back when done (delivery tiers) → while it works the user can wake the device again and ask for progress or redirect; that message interrupts the in-flight turn the way an interrupt works in Claude Code and the agent continues from there. ADR-11 is replaced accordingly; the device channel timeout is handled by always speaking a short acknowledgement first, not by a timer.

37. **Latency red line across the chain** — Jay 2026-10-07: minimise latency end to end with streaming at every stage — streaming STT with interim results and endpointing, streaming LLM first token, sentence-level streaming TTS; speech-end → first audible audio ≤ 1.5 s for quick asks. Barge-in: when the user speaks over a reply, stop playback immediately, `interrupt()` the in-flight turn, and fall back to the previous conversation state (the cut reply is kept only as "interrupted" context, never spoken on); the new utterance continues the same session. The spike `docs/logs/latency-spike-2026-10-07.md` measures STT/LLM/TTS and decides SDK vs hybrid.

38. **E2E cadence** — decided 2026-10-07 (Jay): no per-task voice tests; one full end-to-end session per milestone. All audio items deferred so far roll into the M1 session (feat-voice-integration) when Jay is home.

39. **Quick-ask path** — decided 2026-10-07 (Jay): hybrid by model within one session: quick asks on claude-sonnet-5-5 (thinking on, effort low); tasks on claude-fable-5-1 (effort high, fallbacks); the switch happens per turn in the same session, not across runtimes. ha-mcp policy remains the only security gate.

## Live-stack note 2026-10-07 evening
- Wave 5 deploy rolled back twice: the new readiness check correctly refused to commit because mcp-bridge never became healthy — ha-mcp's MCP endpoint hangs on `tools/list` again (initialize 1.7 s OK, REST OK). Needs an HA-MCP reload in HA (Jay cannot do it right now), then `scripts/setup.sh` again; until then the stack runs the previous build. Root-cause task `fix-hamcp-session-hygiene` queued. The stack runs the previous images; voice without HA tools works.

40. **Agent runtime** — approval REVOKED by Jay 2026-10-07 pending the broad harness comparison (`research/reports/weixue-agent-harness-comparison.md`, running). Performance is the top criterion; if the comparison brings no new candidate, the runtime is chosen on measured performance (currently the in-process loop). No agent-service development until then. Earlier text: replace the Claude Agent SDK with a lightweight in-process loop on the Anthropic Python SDK (tool runner, MCP-as-tools over ha-mcp HTTP, server-side web_search/web_fetch, tool search with deferred loading, memory tool, per-message effort). One process, one SQLite session store for quick asks and tasks. Fallback trigger (3-day spike): first token ≤ 800 ms with tool search (or the 7-tool set + hand-off), clean cancel < 500 ms and steer at tool boundaries without 400s on Fable, web_search pause_turn auto-resumed, one ADR-9 held HA call end to end; on failure the task path reverts to the Agent SDK while quick asks stay direct. Goose rejected (assessment `research/reports/weixue-agent-runtime-assessment.md`). ADR-10 updated.
41. **Web search provider** — default kept (Jay did not object): Anthropic server-side web_search/web_fetch (same key, no new account). Alternatives if wanted later: SearXNG self-hosted, Brave, Tavily, Exa as an MCP tool.

42. **Google STT model and mode** — decided 2026-10-07 (Jay): switch the voice path to `latest_short` streaming (en-US + cmn-Hans-CN), after a 10-sentence Mandarin accuracy check; fall back to chirp_3 on detected Chinese if accuracy degrades. Plus the our-stack mitigations (streaming ASR interface, first-clause TTS, short first sentence, spoken ack during tool calls).

## Deferred to home (device audio needed; HA is reachable from the office via the WireGuard tunnel since 2026-10-07)
- `fix-wave3-firmware` (one-breath wake capture, mute transport, IMU torn-read fix, thinking state, panel send, clock resync): code complete on branch, host tests pass; needs the 4×10 wake trials and mute/panel/TZ checks on the home LAN.
- Audio-only checks from fix-server-review-2 (barge-in / TTS cancel) and fix-wave3-server (announce playback, urgent conversation, wake cue → "Yes" for every cue, long reply with 0 self-aborts): run at home via the feat-voice-integration gate. Everything else passed in the 2026-10-07 live step.

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
