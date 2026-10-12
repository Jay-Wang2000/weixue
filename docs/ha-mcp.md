# Home Assistant over ha-mcp

The voice backend reaches Home Assistant through ha-mcp (ADR-2), the `ha_mcp_tools` component that runs inside HA Core on the HA Green (`192.168.8.142`, port 9584, secret-path mode). The ha-mcp URL is the credential: whoever holds it can call every tool, approve held calls and rewrite the policy. Treat the backend host as holding admin-equivalent access to the house.

## Architecture

```
xiaozhi-esp32-server ──ws /mcp_endpoint/call/──► mcp-endpoint-server ◄──ws /mcp_endpoint/mcp/── mcp-bridge ──HTTP POST──► ha-mcp (HA Green :9584, secret path)
   (LLM tool calls)                                (relay, 127.0.0.1:8004)                     (voice filter)            (tool security policy)
```

`server/mcp-bridge/` is a small Python service (`weixue_bridge`, compose service `mcp-bridge`):

- It connects to the endpoint's tool-side URL and relays each JSON-RPC message from the endpoint to ha-mcp as one Streamable HTTP POST, returning the matching response. Notifications are forwarded without a reply.
- Session: one MCP session per bridge process, opened (`initialize` + `notifications/initialized`) on first use and shared by every endpoint connection and device session. Endpoint `initialize` and `tools/list` requests are answered by the bridge and never reach ha-mcp; `notifications/initialized` from the endpoint is dropped. A `Mcp-Session-Id` from the `initialize` response is sent on every request and ended with an HTTP `DELETE` on shutdown (SIGTERM) and before a replacement session is opened; a 404 for it opens a new session on the next request. ha-mcp's embedded server runs `stateless_http=True` (`ha_mcp_tools/embedded_server.py`): it issues no session id and answers `DELETE` with 405, so the bridge sends none (verified against ha-mcp 8.6.0, 2026-10-10).
- Tool catalog: ha-mcp's `initialize` result and unfiltered tool list are cached in `/app/cache/ha-mcp-tools.json` (`server/data/mcp-bridge/cache/`, `BRIDGE_TOOLS_CACHE` overrides). On every endpoint (re)connect the bridge registers the filtered list from the catalog, so it registers from the cache while ha-mcp's `tools/list` is slow or hanging. A background refresh fetches `tools/list` at start and every 15 min, retries with 5-300 s doubling backoff, and re-registers the list when it changed.
- Bounds per exchange, headers through the last body byte (`weixue_bridge/hamcp.py`): `initialize` 15 s, `tools/list` 20 s, `ping` 10 s, other requests 120 s, `DELETE` 5 s.
- `tools/list` responses are filtered and `tools/call` requests are checked against `server/policy/voice_bridge.json` (see Policy). A refused call is never sent to ha-mcp.
- Every `tools/call` failure goes back to the server as a single-text result with `isError: false`, so the LLM reads it and answers in its own words: the voice-filter refusal; "Home Assistant is unreachable right now." when ha-mcp cannot be reached; and, prefixed `Tool error: `, every ha-mcp `isError` result (`ENTITY_NOT_FOUND`, validation, `USER_APPROVAL_REQUIRED`, `USER_DENIED`) and JSON-RPC error. The vendored server takes only `content[0].text` of a result and replaces an `isError` result with a generic Chinese error that is spoken without an LLM turn. Other requests that fail get a JSON-RPC error. The endpoint connection reconnects with 1-30 s backoff.
- Healthcheck: the file `/tmp/mcp-bridge.healthy` is written when the bridge has registered ha-mcp's tools with the endpoint and ha-mcp has answered a ping; a heartbeat pings ha-mcp every 30 s while the endpoint socket is open and refreshes it only on a success result for its own ping id (an error result, no result or a transport failure removes it). `python -m weixue_bridge.health` passes while that file is under 90 s old.
- Logs carry tool name, outcome and latency (`docker logs mcp-bridge`), never a URL. Session lines name the first 8 characters of the session id, or `stateless`. Every `tools/list` refresh logs `tools= bytes= first_byte_ms= total_ms= session= sessions_opened= changed=`; every 10 successful pings (5 min) log `ha-mcp stats session= sessions_opened= ping_first_byte_ms_p50= ping_first_byte_ms_max= tools_list_first_byte_ms= tools_list_total_ms= tools= tools_age_s=` for the monitor. The same fields plus `state` (`starting|connected|disconnected|stopped`), `announced`, `last_ping_ok_at`, `tools_source` (`cache|live`) and `ts` go to `/app/status/bridge.json` (`server/data/mcp-bridge/status/`, `BRIDGE_STATUS_FILE` overrides), rewritten atomically on endpoint connect and close, after every heartbeat ping and with `stopped` on SIGTERM; it holds no URL and no session id beyond the label. `first_byte_ms` is the first body chunk. `docker inspect mcp-bridge` and `docker compose config` do show `HA_MCP_URL`: never share their output.

| Input | Source |
|---|---|
| `HA_MCP_URL` | `server/.env` (gitignored, 0600), copied from `~/Projects/ha-agent/.mcp.json` |
| Endpoint URL | `server/data/mcp-bridge/endpoint-url` (0600), written by `scripts/setup.sh` from the endpoint's log; `MCP_ENDPOINT` overrides |
| Voice filter | `server/policy/voice_bridge.json`, mounted read-only (`voice_bridge.proposed.json` is the draft that ships with the policy overlay) |

Fill `HA_MCP_URL` without printing it:

```bash
python3 - <<'EOF'
import json, os, re
url = json.load(open(os.path.expanduser("~/Projects/ha-agent/.mcp.json")))["mcpServers"]["home-assistant"]["url"]
p = "server/.env"; text = open(p).read()
text = re.sub(r"^HA_MCP_URL=.*\n?", "", text, flags=re.M).rstrip("\n") + f"\nHA_MCP_URL={url}\n"
open(p, "w").write(text); os.chmod(p, 0o600)
EOF
```

## Verified (2026-10-06)

- Unfiltered, ha-mcp 8.6.0 lists 77 tools (270 KB of JSON). The voice path lists 7 (24 KB); LLM requests then carry about 11.3k input tokens.
- `RUN_LIVE_TESTS=1 python -m pytest -s -k mcp_bridge_live` lists the 7 tools through the endpoint and gets the voice-path refusal for `lock.unlock`. It also drives the vendored server's endpoint client and executor against the live endpoint: the `lock.unlock` refusal and an `ENTITY_NOT_FOUND` from `ha_get_state` both reach the LLM as text (`Action.REQLLM`).
- On session connect the server logs `MCP接入点支持的工具数量: 7` and the `ha_*` names in `当前支持的函数列表`, for both a scripted session and the device.
- Scripted WebSocket text rounds against the live server, Claude Sonnet 5.5, `light.living_room_ceiling_lights` (initially off), all without approval:

| Phrase | Tool calls (bridge latency) | First sentence | TTS stop | HA state |
|---|---|---|---|---|
| "Turn on the living room light." | `ha_search` 99 ms, `ha_call_service` light.turn_on 154 ms | 5.6 s | 8.3 s | on 21:17:51 |
| "Turn off the living room light." | `ha_search` 192 ms; no call, the light had already been switched off at 21:18:01 outside the voice path | 3.8 s | 12.4 s | off |
| "Turn it back on." | `ha_call_service` light.turn_on 147 ms | 8.9 s | 13.7 s | on 21:18:27 |
| "Now turn it off again." | `ha_call_service` light.turn_off 239 ms | 4.5 s | 7.4 s | off 21:18:42 (initial state) |

Failure text through a full LLM turn (scripted rounds, voice filter with `light` only):

| Phrase | Tool calls | Spoken reply | First sentence |
|---|---|---|---|
| "Turn on the working desk plug." | `ha_search` 93 ms; `ha_call_service` switch.turn_on refused by the filter | "I can't switch that plug on by voice. You'll need to do it in the Home Assistant app. ..." | 5.1 s |
| "What is the state of light.weixue_test_absent? ..." | `ha_get_state` ha-mcp `ENTITY_NOT_FOUND` 58 ms | "That light doesn't exist in Home Assistant. ..." | 3.6 s |

## Reconnect

- `docker logs mcp-bridge` should end with `connected; 7 ha-mcp tools on the voice path`; `docker ps` shows `(healthy)`.
- After an HA restart, an ha-mcp reload or a network outage the bridge reconnects by itself; check the log and the health status.
- `ha-mcp tools/list refresh failed: tools/list timed out after 20 s` while pings succeed is the 2026-10-07 symptom (ha-mcp hangs on large responses until HA-MCP is reloaded in HA). In that state `initialize` and the policy REST API still answered; the bridge stays registered and healthy on the cached list while pings succeed and keeps retrying with backoff.
- After `server/data/mcp-endpoint/` is deleted (new endpoint key) or the endpoint image changes, run `./scripts/setup.sh`: it rewrites `data/mcp-bridge/endpoint-url` and `data/.config.yaml` and recreates the bridge and the server.
- After editing `voice_bridge.json` or `HA_MCP_URL`: `docker compose up -d --force-recreate mcp-bridge`.
- The bridge exits non-zero after `ENDPOINT_MAX_FAILURES` consecutive failed endpoint connects (`weixue_bridge/config.py`); `restart: unless-stopped` starts it again.
- A device session reads the tool list when it connects; sessions opened before the bridge came up keep their old list until they reconnect.

## Rotate the secret path

The secret path has been in model context, and the same URL serves Claude Code in `~/Projects/ha-agent`; rotate it before the voice path is used day to day (research §11.4). Rotation is a user step:

1. HA sidebar → HA-MCP panel → rotate the secret path. The old URL stops working for every client.
2. Put the new URL into `~/Projects/ha-agent/.mcp.json` (`mcpServers.home-assistant.url`); restart Claude Code sessions in that repo.
3. Update `HA_MCP_URL` in `server/.env` with the snippet above.
4. `docker compose up -d --force-recreate mcp-bridge`, then check the log and run the live smoke test.

## Policy

Two layers decide what a voice command may do.

### Voice filter (`server/policy/voice_bridge.json`, live)

Applies only to the voice path; it can only narrow what ha-mcp allows.

- `tools`: the 7 tools the voice LLM sees and may call: `ha_search` (find entities by name/area), `ha_get_state`, `ha_get_history`, `ha_list_floors_areas`, `ha_call_service`, `ha_get_todo`, `ha_config_get_calendar_events`. Everything else (configuration writes, automations, dashboards, files, backups, updates, restarts, `ha_bulk_control`) is invisible and refused. This also keeps 246 KB of tool schemas out of every LLM request.
- `call_service_domains`: `ha_call_service` runs only for `light`, the one voice domain the current ha-mcp policy leaves free. Every other domain is disabled on the voice path rather than held: a held call waits `wait_seconds` (180 s live), longer than the server waits for a tool result, so the turn would fail and leave a pending approval that could still run the action later. ha-mcp's policy cannot express "deny", only "hold", so disabling lives here.
- `voice_bridge.proposed.json` (draft) widens `call_service_domains` to `light`, `switch`, `cover`, `media_player`, `climate`, the domains the overlay below frees. It goes live only together with the overlay. Still disabled after that: `lock`, `alarm_control_panel`, `hassio`, `shell_command`, `python_script`, `homeassistant` (restart/stop and generic `homeassistant.turn_*`), `script`, `automation`, `rest_command`, `notify` and every domain not listed. `tests/test_voice_policy.py` checks that the live filter's domains are free under the current policy and that the draft's domains are the overlay's free set.
- `ws_command` (raw WebSocket commands through `ha_call_service`) is always refused.

### ha-mcp policy overlay (`server/policy/voice_policy.proposed.json`)

ha-mcp has one policy for every client, so this overlay changes Claude Code's gates in `~/Projects/ha-agent` as well. Rules are `require_approval`: a call is held when any rule matches. The overlay replaces the single `ha_call_service` rule `domain not_in [light, notify]` of `~/Projects/ha-agent/policy/tool_policy.json`; `wait_seconds` and all other rules stay as in the base (ADR-9: timeouts live in our layer).

| Rule | Effect | Why |
|---|---|---|
| `domain not_in [light, notify, switch, cover, media_player, climate]` | Every other domain stays held, unchanged for Claude Code. | Day-to-day domains free (§11.4); irreversible ones keep the human click for Claude Code and are disabled for voice by the filter. |
| `switch`, `entity_id not_in` the two Meross outlets (`switch.smart_switch_2411…_outlet`, `switch.living_room_donut_light_outlet`) | Only the Meross plugs run free. | Entity-level scope: the other switches are the Matter desk plug, Zigbee2MQTT permit-join, doorbell and Meross over-temperature config switches. A comma-separated pair is held. |
| `switch`/`cover` with `data.entity_id`/`area_id`/`device_id`/`floor_id`/`label_id` present | Held. | Targeting through `data` or by area would bypass the entity scope. |
| `cover`, `entity_id not_in [cover.curtain_partner_1]` | Only the curtain runs free, position included. | No garage door or gate can join the free set by accident. |
| `climate`, `service not_in [turn_on, turn_off, set_temperature, set_hvac_mode]` | Presets, fan and swing modes held. | Bounded climate. |
| `climate`, `data.temperature`/`target_temp_high`/`target_temp_low` `gt 26`, `lt 16`, or a string (`regex "."`) | Setpoints outside 16-26 °C held. | Bounded climate; numeric comparison skips strings, so string values are held. No climate entities exist yet. |
| `media_player` | Free for the whole domain. | No escalation path; no media players exist yet. |

Held on the voice path after the overlay and the draft filter: calls inside the five voice domains that miss the free scope. The test target for `feat-approvals` is `switch.working_desk_plug` (Matter plug, reversible). By the end of phase one nothing on the voice path should be held (§11.4).

`light` stays free for every entity and `notify` stays free for Claude Code, as before.

To apply (user-run; agents must not):

```bash
cd ~/Projects/weixue/server
python3 scripts/render_voice_policy.py ~/Projects/ha-agent/policy/tool_policy.json > /tmp/tool_policy.voice.json
# review, copy over ~/Projects/ha-agent/policy/tool_policy.json, then in ~/Projects/ha-agent:
./scripts/apply-policy.sh
# then widen the voice filter in a weixue commit and recreate the bridge:
cd ~/Projects/weixue/server && git mv -f policy/voice_bridge.proposed.json policy/voice_bridge.json
docker compose up -d --force-recreate mcp-bridge
```

`render_voice_policy.py` refuses an overlay whose removed rule is missing from the base or that sets `wait_seconds`; on a base that already contains the overlay's added rules it prints "already applied" to stderr and emits the base unchanged. `tests/test_voice_policy.py` checks the merged rules against a model of the ha-mcp 8.6.0 evaluator, using frozen ha-agent snapshots (`tests/fixtures/ha_agent_policy_{pre,post}_overlay.json`).

Before applying: the live policy (v13) gates `ha_manage_blueprints`, while `~/Projects/ha-agent/policy/tool_policy.json` still lists the old `ha_import_blueprint`. `apply-policy.sh` PUTs the repo file, so applying it unchanged drops the `ha_manage_blueprints` gate. Rename that rule in the base file first.
