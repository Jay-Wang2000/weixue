# fix-hamcp-session-hygiene evidence (2026-10-10)

## 1. Live ha-mcp, read-only, bounded (home LAN, 22:31 IST)

20 cycles of a fresh HTTP client: `initialize`, `notifications/initialized`, `tools/list`; stop rule: a `tools/list` over max(5 s, 5x the first). One `DELETE` without a session id in cycle 0. URL never printed.

```
00 initialize 200 first=106ms total=109ms 2495B session_id=none ct=text/event-stream
00 tools/list 200 first=110ms total=127ms 261685B ct=text/event-stream notif=202
00 DELETE without session id -> 405 '{"jsonrpc":"2.0","id":null,"error":{"code":-32600,"message":"Method Not Allowed: Session termination not supported"}}'
01 initialize 200 first=20ms total=24ms 2495B session_id=none ct=text/event-stream
... (cycles 02-18 omitted: tools/list first byte 100-154 ms, total 118-176 ms, 261685 B each)
19 tools/list 200 first=100ms total=120ms 261685B ct=text/event-stream notif=202
```

- Every `initialize` returned no `Mcp-Session-Id`; `DELETE` answered 405 "Session termination not supported". ha-mcp's embedded server is built with `http_app(path=..., stateless_http=True)` (homeassistant-ai/ha-mcp `custom_components/ha_mcp_tools/embedded_server.py`, master b274a93), uvicorn `timeout_keep_alive=5`.
- No slowdown in 20 cycles. The MCP layer keeps no per-client session to accumulate, so "sessions never terminated" does not explain the 2026-10-07 hang on this server.

## 2. Local stand-in, stalled readers

MCP SDK 1.30.0 / uvicorn 0.53.0 / starlette 1.7.0 / sse-starlette 3.4.11 (ha-mcp's `uv.lock` pins), stateless, 77 tools, 279 KB `tools/list`. 20 connections that request `tools/list` and never read the body did not slow a fresh `tools/list` (6-7 ms with and without them, and after closing them).

## 3. Bridge process, 50 restarts (SIGTERM each), fake ha-mcp = MCP SDK server

Each cycle: start `python -m weixue_bridge` against a fake endpoint, wait for one `tools/call` reply, SIGTERM. Server counters after the cycle (`active_sessions` = the SDK session manager's open transports).

| Bridge | Server mode | After cycle 1 | After cycle 50 | tools/call replies |
|---|---|---|---|---|
| fix branch | stateful | `{"initialize": 1, "delete": 1, "requests": 6, "active_sessions": 0}` | `{"initialize": 50, "delete": 50, "requests": 300, "active_sessions": 0}` | 50/50 |
| main d8cbb24 | stateful | `{"initialize": 1, "delete": 0, "requests": 4, "active_sessions": 1}` | `{"initialize": 50, "delete": 0, "requests": 200, "active_sessions": 50}` | 50/50 |
| fix branch | stateless (as ha-mcp) | `{"initialize": 1, "delete": 0, "requests": 5, "active_sessions": 0}` | `{"initialize": 50, "delete": 0, "requests": 250, "active_sessions": 0}` | 50/50 |

- Fix branch, stateful: one session per process, 50 `DELETE -> HTTP 200`, 0 open sessions after every cycle. Cycles 2-50 registered from the cache before ha-mcp answered. Bridge log of one cycle:

```
INFO weixue_bridge: ha-mcp http://127.0.0.1:19402 <-> endpoint ws://127.0.0.1:19729
INFO weixue_bridge: tool cache loaded: 77 tools, age_s=0
INFO weixue_bridge: connected; 7 ha-mcp tools on the voice path
INFO weixue_bridge: ha-mcp session 806ecace... opened sessions_opened=1 initialize_first_byte_ms=20
INFO weixue_bridge: ha-mcp tools/list ok tools=77 bytes=279923 first_byte_ms=6 total_ms=6 session=806ecace... sessions_opened=1 changed=no
INFO weixue_bridge: tools/call ha_get_state ok in 13 ms
INFO weixue_bridge: ha-mcp session 806ecace... DELETE -> HTTP 200
INFO weixue_bridge: stopped
```

- main: no `DELETE`, open sessions grow by one per restart (50 after 50).
- Unit tests (`server/tests/test_mcp_bridge.py`): 50 endpoint reconnects in one process share one session (1 `initialize`, 1 `tools/list`, 50 `tools/call`, 1 `DELETE`); 50 `run()` restarts open and `DELETE` one session each; a stalled `tools/list` body fails after its bound; a hanging `tools/list` with a cache still registers tools and serves endpoint `initialize`/`tools/list`/`tools/call`.
