# Feature Spec: Monitor home snapshot (decision 48)
Task: feat-monitor-home-snapshot | Branch: feat/monitor-home-snapshot | Date: 2026-10-11

## Summary
agent-service reads a capped set of Home Assistant entities every 5 minutes through the normal HA gateway (free, read-only ha-mcp calls) into a new `home_snapshot` table; the monitor shows them in its Home tile, on by default, refreshed over the existing SSE.

## Changes
- `weixue_agent/home.py`: job every `AGENT_HOME_SNAPSHOT_S` (300, 0 = off). Default selection: one `ha_get_overview` for lock, climate, cover, light, ordered by domain then entity id, cut at 20; `AGENT_HOME_ENTITIES` (comma list, max 20) replaces it. One bulk `ha_get_state` with trimmed key attributes.
- Schema v3 (additive): `home_snapshot`, `home_snapshot_meta`; rows written only on change; failures keep rows, set `error_code`, log once in `errors`, never raise; a held read withdraws and fails the cycle.
- Monitor: schemas 1-3 supported, `MONITOR_HOME` default 1, name/state/detail/since table, fetched age, stale after 2 periods, failure code; column allowlist extended.
- compose, `.env.example`, docs; `prune` also drops terminal `approvals` older than 30 d (the job adds two rows per cycle).

## Design Decisions
- Locks before climate, covers, lights when the cap bites; no per-domain round robin.
- Freshness lives in the meta row, so unchanged rows are never rewritten.

## Problems & Resolutions
- A held read would leave an HA approval: it is withdrawn when owned; otherwise it lapses at the policy TTL and the cycle reports `held`.

## Open Questions
- Entity names Jay may prefer instead of the default selection (15 today: 14 lights, 1 cover).

## Future Work
- Tile in the device panel; history of state changes.

## Test Coverage
Service: 15 tests (cap and order, override, write-on-change, removal, unreachable/error/malformed/held failures, redaction, loop, prune, v1/v2 migration). Monitor: 45 total, home from fixture rows, stale logic, v2 database, allowlist, default on. Setup/compose tests green (106). Live: `setup.sh` from this branch under the device lock; schema 3, first snapshot 15 entities 0.5 min after start, second cycle wrote 0 rows, `held=0`, approvals `executed` only (`docs/logs/feat-monitor-home-snapshot-live-2026-10-11.log`, screenshots at 360/1280 px).

## Deferred checks
- No device or audio run (no firmware or voice change). Failure and held paths are unit-tested only, not forced on the live stack.
- The live stack now mounts this worktree's `server/data`; the post-merge redeploy from main must precede worktree removal.
