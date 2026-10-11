"""HTTP API: GET-only routes, no forbidden data in any response, live tiles, SSE sections, schema banner."""
import asyncio
import json
import re
import sqlite3

import httpx
import pytest

import fixture_db as fx
from conftest import MONITOR_TOKEN
from agent_monitor.app import STATIC

GET_PATHS = ["/healthz", "/api/overview", "/api/timeline", f"/api/timeline?task={fx.T_LONG}",
             f"/api/tasks/{fx.T_LONG}", f"/api/tasks/{fx.T_OPEN}", "/api/cost?group=chat", "/api/cost?group=task",
             "/api/cost?group=day&days=60", "/api/home", "/api/chats", "/api/errors"]
FORBIDDEN_KEYS = {"ha_token", "args_json", "payload_json", "lease_id", "content_json", "system_text", "tools_json",
                  "response_json", "request_hash", "question_json", "action_json", "decision_json", "speak",
                  "ha_token_id", "last_error", "recovery"}


def keys(value):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k
            yield from keys(v)
    elif isinstance(value, list):
        for v in value:
            yield from keys(v)


async def test_every_route_is_get_only(api):
    for route in api.app.routes:
        methods = getattr(route, "methods", None)
        if methods is not None:
            assert methods <= {"GET", "HEAD"}, (route.path, methods)
    for path in ["/", "/api/overview", "/api/timeline", f"/api/tasks/{fx.T_LONG}", "/api/cost", "/api/home",
                 "/api/chats", "/api/errors", "/api/stream", "/healthz", "/static/app.js"]:
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            resp = await api.request(method, path)
            assert resp.status_code == 405, (method, path, resp.status_code)
    for path in ["/docs", "/openapi.json", "/redoc", "/api/approve", "/api/tasks/x/approve"]:
        assert (await api.get(path)).status_code in (404, 405)


async def test_no_response_carries_forbidden_keys_or_secrets(api):
    for path in GET_PATHS:
        resp = await api.get(path)
        assert resp.status_code == 200, (path, resp.status_code, resp.text[:200])
        body = resp.json()
        assert not set(keys(body)) & FORBIDDEN_KEYS, (path, set(keys(body)) & FORBIDDEN_KEYS)
        for secret in (*fx.SECRETS, MONITOR_TOKEN, "RECOVERY_SECRET", "QUEUED_SECRET"):
            assert secret not in resp.text, (path, secret)


async def test_overview_tiles_from_live_sources(api, upstream):
    overview = (await api.get("/api/overview")).json()
    tiles = {t["id"]: t for t in overview["tiles"]}
    assert set(tiles) == {"device", "agent", "ha_mcp", "bridge"}
    assert tiles["device"]["state"] == "ok" and tiles["device"]["word"] == "polling"
    assert tiles["agent"]["state"] == "ok"
    assert tiles["ha_mcp"]["state"] == "ok" and tiles["ha_mcp"]["value"] == "180 ms" and tiles["ha_mcp"]["age_s"] == 4
    assert tiles["bridge"]["state"] == "ok"
    mcp = overview["mcp"]
    assert (mcp["reachable"], mcp["tool_count"], mcp["policy_version"], mcp["wait_seconds"]) == (True, 77, 3, 8)
    assert mcp["pending_not_ours"] == 2 and [a["approval_id"] for a in mcp["pending_ours"]] == ["apr_3"]
    assert mcp["pending_ours"][0]["age_s"] == 60 and mcp["pending_ours"][0]["expires_in_s"] == 240
    assert overview["counts"]["open_tasks"] == 1 and overview["counts"]["errors_24h"] == 1
    assert overview["db"] == {"state": "ok", "schema": 2, "banner": None} and overview["tz"] == "Europe/Dublin"
    assert {(p, a) for p, a in upstream.auth} >= {("/v1/status", f"Bearer {MONITOR_TOKEN}"),
                                                  ("/weixue/proactive", f"Bearer {MONITOR_TOKEN}"), ("/healthz", None)}


async def test_tunnel_tile_shown_only_in_tunnel_mode(make_app):
    for tunnel, expected in ((False, False), (True, True)):
        app = make_app(tunnel=tunnel)
        await app.state.board.live.refresh()
        ids = {t["id"] for t in app.state.board.overview()["tiles"]}
        assert ("tunnel" in ids) is expected


async def test_agent_down_turns_agent_and_ha_mcp_red(make_app, upstream):
    app = make_app()
    board = app.state.board
    await board.live.refresh()
    upstream.down.add("agent-service")
    await board.live.refresh()
    await board.live.refresh()
    tiles = {t["id"]: t for t in board.overview()["tiles"]}
    assert tiles["agent"]["state"] == "bad"
    # The stored ha_mcp row is 8 s old at NOW; it is the evidence once /v1/status fails.
    assert tiles["ha_mcp"]["state"] == "ok" and tiles["ha_mcp"]["age_s"] == 8


async def test_ha_mcp_red_when_evidence_is_older_than_25_s(make_app, upstream):
    clock = [fx.NOW.timestamp()]
    app = make_app(clock=lambda: clock[0])
    board = app.state.board
    await board.live.refresh()
    clock[0] += 22
    assert {t["id"]: t for t in board.overview()["tiles"]}["ha_mcp"]["state"] == "bad"


async def test_bridge_down_when_the_status_file_age_check_fails(make_app, tmp_path):
    clock = [fx.NOW.timestamp()]
    app = make_app(clock=lambda: clock[0])
    board = app.state.board
    await board.live.refresh()
    assert {t["id"]: t for t in board.overview()["tiles"]}["bridge"]["state"] == "ok"
    clock[0] += 56
    await board.live.refresh()
    bridge = {t["id"]: t for t in board.overview()["tiles"]}["bridge"]
    assert bridge["state"] == "bad" and bridge["code"] == "stale" and bridge["age_s"] == 66
    (tmp_path / "bridge.json").unlink()
    await board.live.refresh()
    assert {t["id"]: t for t in board.overview()["tiles"]}["bridge"]["code"] == "no_status_file"


async def test_device_tile_follows_the_gateway(make_app, upstream):
    app = make_app()
    board = app.state.board
    upstream.devices[fx.DEVICE].update(poll_held=False, last_poll_end_seconds_ago=4.0)
    await board.live.refresh()
    assert {t["id"]: t for t in board.overview()["tiles"]}["device"]["state"] == "bad"
    upstream.devices[fx.DEVICE].update(session_open=True)
    await board.live.refresh()
    assert {t["id"]: t for t in board.overview()["tiles"]}["device"]["word"] == "session"
    upstream.down.add("xiaozhi-esp32-server")
    await board.live.refresh()
    assert {t["id"]: t for t in board.overview()["tiles"]}["device"]["state"] == "bad"


async def test_chats_carry_live_actor_state(api):
    chats = {c["chat_id"]: c for c in (await api.get("/api/chats")).json()["chats"]}
    assert chats[fx.CHAT_B]["busy"] is True and chats[fx.CHAT_B]["queued"] == 1
    assert chats[fx.CHAT_A]["busy"] is False


async def test_home_tile_is_disabled_by_default_and_reports_a_missing_table(make_app):
    assert make_app().state.board.home() == {"enabled": False, "entities": [], "fetched_at": None, "stale": None}
    home = make_app(home=True).state.board.home()
    assert home["enabled"] is True and home["code"] == "no_snapshot_table"


async def test_unknown_schema_shows_the_banner_and_no_rows(make_app, db_path):
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA user_version=7")
    writer.commit()
    writer.close()
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m") as client:
        overview = (await client.get("/api/overview")).json()
        assert overview["db"]["banner"] == "schema 7 not supported"
        assert (await client.get("/api/timeline")).json()["groups"] == []
        health = await client.get("/healthz")
        assert health.status_code == 503 and health.json()["db"] == "schema"


async def test_missing_database_keeps_the_board_up(make_app, tmp_path, monkeypatch):
    app = make_app()
    app.state.board.db.path = str(tmp_path / "gone.db")
    app.state.board.db._local.conn = None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m") as client:
        assert (await client.get("/api/overview")).json()["db"]["state"] == "missing"
        assert (await client.get("/healthz")).status_code == 503


async def test_stream_sends_a_snapshot_then_changed_sections(make_app, db_path):
    app = make_app()
    hub = app.state.hub
    await app.state.board.live.refresh()
    stream = hub.stream()
    snapshot = [await asyncio.wait_for(stream.__anext__(), 5) for _ in range(6)]
    sections = [json.loads(f.split("data: ", 1)[1])["section"] for f in snapshot]
    assert sections == ["overview", "timeline", "cost", "chats", "errors", "home"]
    assert await hub.db_tick() is True
    assert await hub.db_tick() is False
    writer = sqlite3.connect(db_path)
    writer.execute("INSERT INTO tool_calls (tool_use_id, task_id, chat_id, kind, tool, status, started_at) "
                   "VALUES ('tu_new', ?, ?, 'ha', 'ha_get_state', 'ok', '2026-10-26T11:59:00.000Z')",
                   (fx.T_OPEN, fx.CHAT_B))
    writer.commit()
    writer.close()
    assert await hub.db_tick() is True
    frame = await asyncio.wait_for(stream.__anext__(), 5)
    message = json.loads(frame.split("data: ", 1)[1])
    assert frame.startswith("event: section\n") and message["section"] == "timeline"
    group = next(g for g in message["data"]["groups"] if g["task_id"] == fx.T_OPEN)
    assert "tu_new" in {c["tool_use_id"] for c in group["tool_calls"]}
    await stream.aclose()
    assert hub.clients == set()


async def test_index_and_static_files_are_self_contained(api):
    index = await api.get("/")
    assert index.status_code == 200 and "default-src 'self'" in index.headers["content-security-policy"]
    for name in ("app.js", "app.css"):
        resp = await api.get(f"/static/{name}")
        assert resp.status_code == 200
    for path in STATIC.iterdir():
        text = path.read_text()
        assert not re.search(r"https?://", text.replace("http://www.w3.org/2000/svg", "")), path.name
        assert "innerHTML" not in text, path.name


def test_settings_from_env_never_echo_values():
    from agent_monitor.config import ConfigError, load_settings
    settings = load_settings({"MONITOR_TOKEN": MONITOR_TOKEN, "WG_TUNNEL": "1", "TZ": "UTC"})
    assert settings.tunnel and settings.tz == "Europe/Dublin" and settings.ha_probe_s == 10
    assert load_settings({"AGENT_HA_PROBE_S": "30"}).ha_probe_s == 30
    for env in ({"MONITOR_TOKEN": "abc123"}, {"AGENT_SERVICE_URL": "http://agent/private_abc"},
                {"MCP_ENDPOINT": "ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=xyz"}):
        with pytest.raises(ConfigError) as e:
            load_settings(env)
        assert not any(v in str(e.value) for v in env.values())
