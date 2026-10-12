# Feature Spec: feat-ubuntu-migration
Task: feat-ubuntu-migration | Branch: feat/ubuntu-migration | Date: 2026-10-12

## Summary
The stack can now run on `jay@192.168.8.125`. The WireGuard sidecar is gone, `setup.sh` works on Linux, and the Mac scripts gain an SSH remote mode. Staging stopped halfway: the permission classifier blocked copying the `.env`, the SA JSON and the data to the server.

## Changes
- Compose: `wg` and everything wired to it are removed, along with `server/wg/` and its tests. The `console` profile stays.
- `setup.sh`:
  - `LAN_IP` must be an address on the host (`ip -4 addr`, falling back to `ifconfig`).
  - It refuses `WG_TUNNEL`, the `tunnel` profile and a set `WEIXUE_SERVER_SSH`.
  - Backup and rollback are unchanged.
- `scripts/lib/remote_docker.{sh,py}`: run docker locally, or over ssh (`BatchMode`) with each argument quoted when `WEIXUE_SERVER_SSH` is set. Used by AgentClient, `approval_gate`, `spike_whisper` and the `audio_session.sh` fixture base.
- Docs: runbook, server.md Deployment host and Remote mode, CLAUDE.md, ADR-4, and the agent-service, monitor, ha-mcp and firmware docs.

## Design Decisions
- The server keeps `BIND_IP=0.0.0.0`, as the Mac does.

## Open Questions
- Copying secrets to the server needs Jay's approval, or Jay runs runbook §2–3.
- Tailscale or a router VPN for the Mac in the office? Decision needed from Jay.
- The deploy key (runbook option A) is a GitHub change.

## Cutover (orchestrator)
```
cd ~/Projects/weixue/server && docker compose stop
rsync -a --delete -e "$SSH" data/agent-service/db/ $SRV:/home/jay/weixue/server/data/agent-service/db/   # + memory/, weixue-gateway/
$SSH $SRV 'cd /home/jay/weixue/server && ./scripts/setup.sh'
OTA_URL=http://192.168.8.125:8003/xiaozhi/ota/ .venv/bin/python firmware/tools/usb_provision.py   # under the device lock
scripts/audio_session.sh --no-flash -- --items one_breath --count 5
```
Rollback: stop the server stack, remove `WEIXUE_SERVER_SSH` from the Mac's `.env`, run `docker compose up -d` on the Mac and point the device's OTA URL back at the Mac.

## Future Work
- Remove the dead tunnel code: bridge `TUNNEL_IFACE`, agent-service wg0 and the monitor's tunnel tile.

## Test Coverage
- Server suite: 1030 passed, including the new LAN_IP, ifconfig, refusal and compose tests.
- Scripts: 89 unit tests pass, 9 of them for the remote helper (fake ssh round-trip). They also pass on the server's bash 5.2.
- A dry run of `setup.sh` on the server is in `docs/logs/feat-ubuntu-migration-setup-dryrun-2026-10-12.log`.
- On the server: all four x86_64 images build. AgentClient over SSH reached a scratch container (200, token OK, logs).

Deferred checks:
- On the server: `setup.sh` readiness, the text turn, and `compose stop`.
- The models are still in `/home/jay/weixue-staging/models`.
- Cutover voice check.
