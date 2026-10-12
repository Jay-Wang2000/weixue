# Feature Spec: Monitor redesign (Logbook direction), HA-embeddable
Task: feat-monitor-ui-v2 | Branch: feat/monitor-ui-v2 | Date: 2026-10-12

## Summary
The monitor page is rebuilt on the research's final direction (03-final): sticky verdict, held-approval card, five-cell band, since-you-last-looked summary, searchable feed with per-task traces. It can be embedded in Home Assistant (decision 79): a token gate, CSP frame-ancestors, `?embed=1`, and a LAN bind setting. Phase 3 (`/api/health/history`, connection lanes) is included.

## Changes
- `static/`: new index.html, app.css (light and dark tokens, phone layout) and app.js. IBM Plex Latin-1 subsets are vendored (120 KB, OFL).
- API: `/api/problems` (severity map in `severity.py`), `/api/since`, `/api/health/history` (`lanes.py`). Timeline gains `q`, `segment` and segment counts. Overview gains `verdict`, `last_decision` and `turns_today`. New SSE `problems` section and `heartbeat` event.
- Gate: `MONITOR_TOKEN` is required on `/` and `/api/` (`?token=` or bearer), checked with constant-time compare. `MONITOR_FRAME_ANCESTORS` is validated (default `'none'`). Compose binds `${MONITOR_BIND:-127.0.0.1}`.
- Docs: agent-monitor.md rewritten with the HA card YAML; server.md, the runbook and .env.example updated.

## Design Decisions
- The verdict and severity are computed on the server so they can be tested. Notices are `model/transcript`, `model_fallback` and `retention`.
- Lanes only cover the probes stored in `health_probes`. A gap of more than 660 s shows as unknown.
- Fonts are vendored (open decision 4 in the report), and the held card gives "decide" as text only (decision 5).

## Open Questions
- The iframe URL exposes `MONITOR_TOKEN`, which is also the read scope for agent-service `/v1/status` and the LAN-exposed gateway `GET /weixue/proactive`. Should there be a separate view-only token? Recommendation: yes, as a small follow-up.

## Test Coverage
64 monitor tests pass. They cover the token gate, CSP and its validation, embed mode, severity map, verdict, since counts, segments and search, lanes, and 4.5:1 contrast of every token pair in both themes. Of the setup-script tests, 92 pass; 2 need `websockets`, which is not installed in the scratch venv. Screenshots (scratch DB; desktop and phone; idle, held, trace, light/embed) are in docs/logs/monitor-ui-v2-*.png, compared with 03-final in the .log. Heights: 2,647 px desktop and 4,393 px phone.

Deferred checks (orchestrator):
- Deploy to the server with `MONITOR_BIND` and `MONITOR_FRAME_ANCESTORS` set.
- Check the Webpage card inside HA in a browser and the HA phone app, plus the light/dark follow.
- Look at the live data on the board.
- Run a Docker image build.
- Confirm that `.env` values reach compose.
