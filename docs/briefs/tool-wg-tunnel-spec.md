# Feature Spec: WireGuard tunnel sidecar for the ha-mcp bridge
Task: tool-wg-tunnel | Branch: tool/wg-tunnel | Date: 2026-10-07

## Summary
Optional compose profile `tunnel`: a pinned WireGuard client sidecar `wg` whose network namespace `mcp-bridge` joins when `WG_TUNNEL=1`, so only the bridge's HA traffic leaves through the home router. `setup.sh` keeps `COMPOSE_PROFILES=tunnel` in `server/.env` while the tunnel is on, so plain `docker compose` commands keep working. The orchestrator deployed this branch to the live stack and verified it end to end from the hotspot; the fix-pass changes (wg retry, bridge exit, profile in `.env`) are verified by tests and scratch containers and go live with the next redeploy.

## Changes
- `server/wg/`: Alpine 3.23.4 (digest) + `wireguard-tools=1.0.20250521-r1`; entrypoint brings `wg0` up from a DNS-stripped tmpfs copy, retries a failed `wg-quick up` every 10 s with one redacted line (keys, IPv4/IPv6, endpoints, quoted values), sets 25 s keepalive; healthcheck: handshake under 180 s, 60 s start period.
- `docker-compose.yml`: `wg` (profile `tunnel`, NET_ADMIN, `src_valid_mark=1`, `wg0.conf` read-only, `create_host_path: false`); `mcp-bridge` `network_mode: ${WG_TUNNEL:+service:wg}`.
- `setup.sh`: `WG_TUNNEL` empty or `1` only; with `1` checks `wg0.conf` exists and is 0600 (never reads it), adds `tunnel` to `COMPOSE_PROFILES` in `.env` (quoted values read, unquoted written) and recreates `wg`; when empty strips it and removes the `wg` container.
- `mcp-bridge` (both modes): exits 1 after `ENDPOINT_MAX_FAILURES` (12, 211 s of backoff) consecutive failed endpoint connects, or when `eth0`, present at start, is gone; the restart policy starts it again, in tunnel mode inside `wg`'s current namespace.
- Docs: `docs/server.md` Office / off-LAN mode (both `.env` lines; after a manual `wg` restart the bridge rejoins on its own, `docker restart mcp-bridge` only skips the wait), `docs/ha-mcp.md` reconnect notes, `.env.example`.

## Design Decisions
- Own image instead of linuxserver/wireguard: no s6/PUID, no writes to the config path, DNS lines dropped so Docker's resolver keeps `mcp-endpoint-server` resolvable.
- Profile carried in `.env` rather than per-command flags; compose treats any non-empty `WG_TUNNEL` as on, `setup.sh` rejects anything but `1`.

## Problems & Resolutions
- A restarted `wg` leaves the bridge in a namespace with `lo` only (reproduced on scratch containers); fixed by the bridge exit conditions above plus the wg retry loop, which keeps `wg` from restart-looping on boot DNS failures.

## Open Questions
- None.

## Future Work
- Proactive webhook inbound through the tunnel.

## Test Coverage
Full suite in the worktree venv (Python 3.10): 315 passed, 10 skipped (live/credential gated). New: `setup.sh` success path with stub docker/git/curl/python3 (profile written, kept with `console`, quoted values, stripped when off, `wg` recreated when on and removed when off), conf missing/0644/invalid values; entrypoint `redact` and retry loop; healthcheck at 5/179/181 s and never; compose profile from an env file; bridge exit after N failures, reset on connect, no exit while only ha-mcp fails, `eth0` loss; a scratch `--network container:` pair rejoined the new namespace by itself after `docker restart`. Live e2e (orchestrator deploy of the first revision, two testers): handshake ages 9-84 s, `wg` and `mcp-bridge` healthy with 0 restarts, bridge `connected; 7 ha-mcp tools`, `tools/list` 77 tools and read-only `ha_get_state sun.sun` through the tunnel, no service call; scratch containers confirmed the retry loop and redaction. Logs: `docs/logs/tool-wg-tunnel-{probe,e2e}-2026-10-07.log`. No device voice test (office mode, no firmware change).
