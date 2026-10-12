# Feature Spec: Separate view-only token for the monitor board
Task: feat-monitor-view-token | Branch: feat/monitor-view-token | Date: 2026-10-12

## Summary
Decision 83 = A. `MONITOR_VIEW_TOKEN` is the only credential the board page and its `/api` routes accept (`?token=` or bearer, constant-time compare). `MONITOR_TOKEN` keeps its agent-service `/v1/status` and gateway proactive scopes and no longer opens the board, so the HA card URL exposes nothing but the board.

## Changes
- `config.py`: `view_token` (>= 32 chars, must differ from `MONITOR_TOKEN`, else the monitor refuses to start) and `local_only` (`MONITOR_BIND` is `127.0.0.1`).
- `app.py`: gate uses `board_access_ok`; an unset view token answers 401 unless `local_only`.
- `setup.sh`: generates `MONITOR_VIEW_TOKEN` once (64 hex), rejects an existing value that is short or equals another token, prints the view URL with it.
- `docker-compose.yml`: agent-monitor gets `MONITOR_VIEW_TOKEN` and `MONITOR_BIND`; no other service sees the view token.
- `.env.example`, `docs/agent-monitor.md` (card YAML, localhost rule, trust boundary), `docs/server.md`.

## Design Decisions
- The localhost rule reads `MONITOR_BIND` (passed into the container), because inside the container the listen address is always 0.0.0.0.
- The deploy summary prints the view URL with the token, as the task requires; the token is by design visible to anyone who can open the HA dashboard.

## Test Coverage
Monitor suite (68 passed): view token accepted by query and bearer, `MONITOR_TOKEN` rejected on page, API and stream, unset token gives 401 and is open only with a 127.0.0.1 bind, settings validation never echoes values. `test_setup_scripts.py`: generation once, 64 hex, distinct from the other tokens, URL printed, bad existing value refused, compose environment.

## Deferred Checks
- Deploy with `MONITOR_BIND=192.168.8.125` and the HA card with the view token: orchestrator.
- Live check that `MONITOR_TOKEN` gets 401 on the deployed board: after that deploy.
