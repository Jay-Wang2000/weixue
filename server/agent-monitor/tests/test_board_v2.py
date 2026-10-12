"""Token gate, CSP frame-ancestors, embed mode, severity map, verdict, since-you-last-looked, segments, lanes,
and the colour tokens' contrast."""
import json
import re
import sqlite3
from datetime import timedelta

import httpx
import pytest

import fixture_db as fx
from conftest import MONITOR_TOKEN, VIEW_TOKEN
from agent_monitor import lanes, severity
from agent_monitor.app import STATIC
from agent_monitor.config import ConfigError, load_settings

AUTH = {"Authorization": f"Bearer {VIEW_TOKEN}"}


def client(app, headers=None):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://m", headers=headers)


def write(db_path, sql, rows):
    writer = sqlite3.connect(db_path)
    writer.executemany(sql, rows)
    writer.commit()
    writer.close()


def add_errors(db_path, rows):
    write(db_path, "INSERT INTO errors (ts, source, code, message, task_id) VALUES (?, ?, ?, ?, ?)", rows)


# ---------------------------------------------------------------------- access


async def test_page_and_api_need_the_view_token(make_app):
    app = make_app()
    async with client(app) as c:
        for path in ("/", "/?embed=1", "/api/overview", "/api/timeline", f"/api/tasks/{fx.T_LONG}", "/api/stream",
                     "/api/problems", "/api/since", "/api/health/history"):
            resp = await c.get(path)
            assert resp.status_code == 401, path
            assert VIEW_TOKEN not in resp.text and MONITOR_TOKEN not in resp.text
        for bad in ({"token": "x" * 40}, {"token": VIEW_TOKEN[:-1]}):
            assert (await c.get("/api/overview", params=bad)).status_code == 401
        assert (await c.get("/api/overview", headers={"Authorization": "Bearer nope"})).status_code == 401
        assert (await c.get("/api/overview", headers={"Authorization": f"Basic {VIEW_TOKEN}"})).status_code == 401
        assert (await c.get("/api/overview", params={"token": VIEW_TOKEN})).status_code == 200
        assert (await c.get("/", params={"token": VIEW_TOKEN})).status_code == 200
        assert (await c.get("/api/overview", headers=AUTH)).status_code == 200
        unauth = await c.get("/api/overview")
        assert unauth.headers["www-authenticate"] == "Bearer"
        assert "frame-ancestors 'none'" in unauth.headers["content-security-policy"]
        for path in ("/healthz", "/static/app.js", "/static/app.css", "/static/fonts/IBMPlexSans-Regular-Latin1.woff2"):
            assert (await c.get(path)).status_code in (200, 503), path


async def test_monitor_token_does_not_open_the_board(make_app):
    app = make_app()
    async with client(app) as c:
        mon = {"Authorization": f"Bearer {MONITOR_TOKEN}"}
        for path in ("/", "/api/overview", "/api/stream"):
            assert (await c.get(path, params={"token": MONITOR_TOKEN})).status_code == 401, path
            assert (await c.get(path, headers=mon)).status_code == 401, path


@pytest.mark.parametrize("local_only", [False, True])
async def test_unset_view_token_is_not_replaced_by_the_monitor_token(make_app, local_only):
    app = make_app(view_token="", local_only=local_only)
    async with client(app) as c:
        resp = await c.get("/api/overview", headers={"Authorization": f"Bearer {MONITOR_TOKEN}"})
        assert resp.status_code == (200 if local_only else 401)


def test_stream_accepts_the_token_in_the_query():
    from starlette.requests import Request
    from agent_monitor.app import token_ok

    def request(query=b"", headers=()):
        return Request({"type": "http", "method": "GET", "path": "/api/stream", "query_string": query,
                        "headers": list(headers)})
    assert token_ok(request(f"token={VIEW_TOKEN}".encode()), VIEW_TOKEN)
    assert token_ok(request(headers=[(b"authorization", f"bearer {VIEW_TOKEN}".encode())]), VIEW_TOKEN)
    assert not token_ok(request(b"token="), VIEW_TOKEN)
    assert not token_ok(request(), VIEW_TOKEN)
    assert not token_ok(request(), "")
    assert not token_ok(request(b"token="), "")


async def test_without_a_view_token_the_gate_stays_closed_unless_bound_to_localhost(make_app):
    closed = make_app(view_token="")
    async with client(closed) as c:
        assert (await c.get("/")).status_code == 401
        assert (await c.get("/api/overview")).status_code == 401
        assert (await c.get("/api/overview", params={"token": ""})).status_code == 401
        assert (await c.get("/healthz")).status_code in (200, 503)
    local = make_app(view_token="", local_only=True)
    async with client(local) as c:
        assert (await c.get("/")).status_code == 200
        assert (await c.get("/api/overview")).status_code == 200
    with_token = make_app(local_only=True)
    async with client(with_token) as c:
        assert (await c.get("/api/overview")).status_code == 401


def test_view_token_settings():
    view = "v" * 40
    assert load_settings({}).view_token == "" and not load_settings({}).local_only
    assert load_settings({"MONITOR_VIEW_TOKEN": view, "MONITOR_TOKEN": "m" * 40}).view_token == view
    assert load_settings({"MONITOR_BIND": "127.0.0.1"}).local_only
    for bind in ("", "192.168.8.125", "0.0.0.0", "localhost"):
        assert not load_settings({"MONITOR_BIND": bind}).local_only
    for env in ({"MONITOR_VIEW_TOKEN": "tiny"}, {"MONITOR_VIEW_TOKEN": view, "MONITOR_TOKEN": view}):
        with pytest.raises(ConfigError) as e:
            load_settings(env)
        assert view not in str(e.value) and "tiny" not in str(e.value)


# ---------------------------------------------------------------------- CSP and embed


async def test_frame_ancestors_follow_the_setting(make_app):
    app = make_app(frame_ancestors="http://192.168.8.142:8123")
    async with client(app, AUTH) as c:
        for path in ("/", "/api/overview", "/static/app.js"):
            csp = (await c.get(path)).headers["content-security-policy"]
            assert "frame-ancestors http://192.168.8.142:8123;" in csp, path
            assert "default-src 'self'" in csp and "script-src 'self'" in csp


def test_frame_ancestors_setting_is_validated_and_never_echoed():
    assert load_settings({}).frame_ancestors == "'none'"
    assert load_settings({"MONITOR_FRAME_ANCESTORS": "  "}).frame_ancestors == "'none'"
    assert load_settings({"MONITOR_FRAME_ANCESTORS": "http://192.168.8.142:8123"}).frame_ancestors == \
        "http://192.168.8.142:8123"
    assert load_settings({"MONITOR_FRAME_ANCESTORS": "'self' https://ha.example:8123"}).frame_ancestors == \
        "'self' https://ha.example:8123"
    for bad in ("*", "http://x; script-src *", "http://ha/path", "'none' http://ha:8123", "javascript:alert(1)",
                "http://ha:8123/?token=private_abc"):
        with pytest.raises(ConfigError) as e:
            load_settings({"MONITOR_FRAME_ANCESTORS": bad})
        assert bad not in str(e.value)


async def test_embed_mode_marks_the_page(api):
    plain = (await api.get("/")).text
    embedded = (await api.get("/?embed=1")).text
    assert 'data-embed="0"' in plain and 'data-embed="1"' not in plain
    assert 'data-embed="1"' in embedded
    assert VIEW_TOKEN not in embedded and MONITOR_TOKEN not in embedded
    css = (STATIC / "app.css").read_text()
    assert re.search(r'html\[data-embed="1"\] \.chrome \{ display: none', css)
    html = (STATIC / "index.html").read_text()
    for chrome in ('class="brand chrome"', 'class="clock chrome"', 'class="theme chrome"', '<footer class="chrome"'):
        assert chrome in html


def test_page_has_no_inline_styles_or_scripts():
    html = (STATIC / "index.html").read_text()
    assert " style=" not in html and "<script>" not in html and "<style" not in html
    js = (STATIC / "app.js").read_text()
    assert 'setAttribute("style"' not in js and "cssText" not in js


# ---------------------------------------------------------------------- severity map and problems


def test_severity_map():
    assert severity.severity("model", "transcript") == "notice"
    assert severity.severity("model", "model_fallback") == "notice"
    assert severity.severity("home_snapshot", "ha_unreachable") == "failure"
    assert severity.severity("turn", "max_turns") == "failure"
    assert severity.severity("new_source", "new_code") == "failure"
    assert {"source": "model", "code": "transcript", "severity": "notice"} in severity.severity_map()


async def test_problems_split_failures_and_notices(api, db_path):
    now = fx.NOW
    rows = [(fx.ts(now - timedelta(minutes=50 - i)), "model", "transcript",
             json.dumps({"path": "messages.1", "reason": "prefix_binding_mismatch"}), fx.T_LONG) for i in range(3)]
    rows += [(fx.ts(now - timedelta(minutes=m)), "home_snapshot", "ha_unreachable", None, None) for m in (40, 30, 5 * 60)]
    add_errors(db_path, rows)
    body = (await api.get("/api/problems")).json()
    groups = {(g["source"], g["code"]): g for g in body["groups"]}
    assert body["failures"] == 4 and body["notices"] == 3 and body["rows"] == 7
    assert [g["severity"] for g in body["groups"]] == ["failure", "failure", "notice"]
    home = groups[("home_snapshot", "ha_unreachable")]
    assert home["count"] == 3 and home["recovered"] is True
    assert [w["count"] for w in home["windows"]] == [1, 2]
    note = groups[("model", "transcript")]
    assert note["reasons"] == [{"reason": "prefix_binding_mismatch", "count": 3}] and note["task_id"] == fx.T_LONG
    assert groups[("ha", "timeout")]["windows"][0]["task_ids"] == [fx.T_LONG]
    assert "private_abc" not in json.dumps(body) and "token=xyz" not in json.dumps(body)
    assert ("model", "overloaded") not in groups


async def test_problems_section_is_streamed(api):
    hub = api.app.state.hub
    await hub.db_tick()
    data = json.loads(hub.cache["problems"])["data"]
    assert data["failures"] == 1 and data["hours"] == 24


# ---------------------------------------------------------------------- verdict


async def test_verdict_needs_a_look_while_a_call_is_held(api):
    verdict = (await api.get("/api/overview")).json()["verdict"]
    assert verdict == {"state": "attention", "held": 1, "down": []}


async def test_verdict_all_good_without_holds_and_needs_a_look_when_a_connection_is_down(make_app, db_path, upstream):
    write(db_path, "UPDATE approvals SET state = ?", [("approved",)])
    app = make_app()
    board = app.state.board
    await board.live.refresh()
    assert board.overview()["verdict"] == {"state": "ok", "held": 0, "down": []}
    upstream.down.add("xiaozhi-esp32-server")
    await board.live.refresh()
    assert board.overview()["verdict"] == {"state": "attention", "held": 0, "down": ["Device"]}


async def test_overview_carries_the_last_decision_and_turns_today(api):
    o = (await api.get("/api/overview")).json()
    last = o["mcp"]["last_decision"]
    assert (last["tool"], last["approval_decision"], last["decided_via"]) == ("ha_call_service", "deny", "voice")
    assert o["counts"]["turns_today"] == 4


# ---------------------------------------------------------------------- since you last looked


async def test_since_defaults_to_local_midnight(api):
    body = (await api.get("/api/since")).json()
    assert body["default"] is True and body["since"] == "2026-10-26T00:00:00.000Z"
    assert (body["turns"], body["chats"], body["tasks"]) == (4, 2, 3)
    assert (body["cancelled"], body["failed"], body["open"]) == (1, 0, 1)
    assert body["ha_changes"] == 1
    assert body["approvals"] == {"decided": 2, "approved": 1, "denied": 1}
    assert abs(body["cost_usd"] - 0.41) < 1e-9
    assert body["failures"] == 1 and body["notices"] == 0
    assert [(p["source"], p["code"], p["count"]) for p in body["problems"]] == [("ha", "timeout", 1)]
    kinds = {t["task_id"]: t["kind"] for t in body["pulse"]}
    assert kinds == {fx.T_LONG: "held", fx.T_CANCEL: "cancel", fx.T_OPEN: "held"}


async def test_since_counts_only_what_came_after_the_last_look(api, db_path):
    last_look = fx.ts(fx.NOW - timedelta(minutes=90))
    body = (await api.get("/api/since", params={"since": last_look})).json()
    assert body["default"] is False and body["since"] == last_look
    assert (body["turns"], body["chats"], body["tasks"]) == (1, 1, 1)
    assert body["ha_changes"] == 0 and body["approvals"]["decided"] == 0 and body["cancelled"] == 0
    assert body["failures"] == 1
    add_errors(db_path, [(fx.ts(fx.NOW - timedelta(minutes=10)), "home_snapshot", "ha_unreachable", None, None)])
    body = (await api.get("/api/since", params={"since": last_look})).json()
    assert body["failures"] == 2 and any(t["kind"] == "bad" and t["task_id"] is None for t in body["pulse"])
    assert (await api.get("/api/since", params={"since": "yesterday"})).status_code == 422


# ---------------------------------------------------------------------- segments and search


async def test_timeline_segments_and_search(api):
    body = (await api.get("/api/timeline")).json()
    assert body["segments"] == {"everything": 4, "ha": 2, "approvals": 2, "failed": 0, "cancelled": 1}
    ids = lambda b: [g["task_id"] for g in b["groups"]]  # noqa: E731
    assert ids((await api.get("/api/timeline", params={"segment": "approvals"})).json()) == [fx.T_OPEN, fx.T_LONG]
    assert ids((await api.get("/api/timeline", params={"segment": "cancelled"})).json()) == [fx.T_CANCEL]
    searched = (await api.get("/api/timeline", params={"q": "SUNSET"})).json()
    assert ids(searched) == [fx.T_LONG] and searched["segments"]["everything"] == 1
    assert ids((await api.get("/api/timeline", params={"q": "100%_"})).json()) == []
    assert ids((await api.get("/api/timeline", params={"q": "weather", "chat": fx.CHAT_B})).json()) == [fx.T_CANCEL]
    assert (await api.get("/api/timeline", params={"segment": "bogus"})).status_code == 422


# ---------------------------------------------------------------------- connection lanes


def test_lane_segments_cover_the_window():
    def row(t, ok):
        return {"ts": fx.ts(fx.NOW - timedelta(seconds=t)), "ok": ok, "latency_ms": 5}
    end = fx.NOW.timestamp()
    start = end - 3600
    segs = lanes.segments([row(4000, True), row(1800, False), row(1700, True)], start, end)
    assert [s["state"] for s in segs] == ["ok", "unknown", "bad", "ok", "unknown"]
    assert segs[0]["from"] == start and segs[-1]["to"] == end
    assert all(a["to"] == b["from"] for a, b in zip(segs, segs[1:]))
    assert segs[1] == {"from": end - 4000 + lanes.PROBE_ROW_GAP_S, "to": end - 1800, "state": "unknown"}
    assert lanes.segments([], start, end) == [{"from": start, "to": end, "state": "unknown"}]


async def test_health_history_lanes(api, db_path):
    write(db_path, "INSERT INTO health_probes (ts, probe, ok, latency_ms, detail_json) VALUES (?, 'ha_mcp', ?, 20, '{}')",
          [(fx.ts(fx.NOW - timedelta(hours=30)), 1), (fx.ts(fx.NOW - timedelta(minutes=20)), 0),
           (fx.ts(fx.NOW - timedelta(minutes=19)), 1)])
    body = (await api.get("/api/health/history", params={"hours": 24})).json()
    by = {lane["probe"]: lane for lane in body["lanes"]}
    assert list(by) == ["ha_mcp", "mcp_endpoint"] and by["ha_mcp"]["label"] == "ha-mcp"
    states = [s["state"] for s in by["ha_mcp"]["segments"]]
    assert states == ["unknown", "bad", "ok", "unknown", "ok"]
    assert by["ha_mcp"]["flips"] == 2
    assert by["ha_mcp"]["segments"][0]["from"] == body["from"] and by["ha_mcp"]["segments"][-1]["to"] == body["to"]


# ---------------------------------------------------------------------- colour tokens


def _tokens(block):
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9A-Fa-f]{6})", block))


def _luminance(hex_):
    channels = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _ratio(a, b):
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_text_tokens_meet_4_5_to_1_on_every_fill_in_both_themes():
    css = (STATIC / "app.css").read_text()
    light = _tokens(css[css.index(":root {"):css.index("@media (prefers-color-scheme: dark)")])
    auto_dark = {**light, **_tokens(css[css.index("@media (prefers-color-scheme: dark)"):css.index(':root[data-theme="dark"]')])}
    forced = css[css.index(':root[data-theme="dark"]'):]
    dark = {**light, **_tokens(forced[:forced.index("}")])}
    assert auto_dark == dark
    texts = ("ink", "ink-2", "ink-3", "accent", "ok", "warn", "bad")
    fills = ("bg", "surface", "surface-2", "warn-bg", "bad-bg", "sel-bg")
    for name, theme in (("light", light), ("dark", dark)):
        for t in texts:
            for f in fills:
                assert _ratio(theme[t], theme[f]) >= 4.5, (name, t, f, round(_ratio(theme[t], theme[f]), 2))
