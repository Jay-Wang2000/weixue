"""Tile rules with a fake clock."""
from agent_monitor import tiles

NOW = 10_000.0


def device(**d):
    base = {"session_open": False, "poll_held": False, "last_poll_end_seconds_ago": None}
    return {"devices": {"7c:0c:5f:42:4b:fc": {**base, **d}}}


def test_device_is_green_with_a_session_or_a_held_poll_and_red_3_s_after_the_poll_ended():
    assert tiles.device(device(session_open=True), None, 0)["state"] == "ok"
    assert tiles.device(device(poll_held=True, last_poll_end_seconds_ago=40), None, 0)["word"] == "polling"
    assert tiles.device(device(last_poll_end_seconds_ago=2.9), None, 0)["state"] == "ok"
    red = tiles.device(device(last_poll_end_seconds_ago=3.1), None, 0)
    assert red["state"] == "bad" and red["word"] == "offline" and red["age_s"] == 3.1
    assert tiles.device(device(), None, 0)["state"] == "bad"
    assert tiles.device(None, "timeout", 5)["code"] == "timeout"
    assert tiles.device({"devices": {}}, None, 0)["code"] == "no_device_seen"


def test_agent_turns_red_after_two_failures():
    ok = {"ok": True, "db": True, "model": True, "ha_mcp": True}
    assert tiles.agent(ok, 0, None, 1)["state"] == "ok"
    assert tiles.agent(ok, 1, "timeout", 3)["state"] == "ok"
    assert tiles.agent(ok, 2, "timeout", 5)["state"] == "bad"
    assert tiles.agent(None, 1, "ConnectError", None)["state"] == "unknown"
    warn = tiles.agent({"ok": False, "db": True, "model": True, "ha_mcp": False}, 0, None, 1)
    assert warn["state"] == "warn" and warn["code"] == "ha_mcp"
    assert tiles.agent({"ok": False, "db": False}, 0, None, 1)["state"] == "bad"


def test_ha_mcp_red_on_failure_or_25_s_age_amber_when_slow_or_tools_stale():
    probe = {"ok": True, "latency_ms": 300, "ts": NOW - 10}
    assert tiles.ha_mcp(probe, NOW, 10)["state"] == "ok"
    assert tiles.ha_mcp({**probe, "ts": NOW - 24.9}, NOW, 10)["state"] == "ok"
    stale = tiles.ha_mcp({**probe, "ts": NOW - 25.1}, NOW, 10)
    assert stale["state"] == "bad" and stale["word"] == "stale"
    assert tiles.ha_mcp({**probe, "ts": NOW - 40}, NOW, 30)["state"] == "ok"
    assert tiles.ha_mcp({**probe, "ts": NOW - 46}, NOW, 30)["state"] == "bad"
    assert tiles.ha_mcp({**probe, "ok": False, "error": "ping timed out after 5 s"}, NOW, 10)["code"].startswith("ping")
    assert tiles.ha_mcp({**probe, "latency_ms": 2500}, NOW, 10)["state"] == "warn"
    assert tiles.ha_mcp(probe, NOW, 10, tools_age_s=4000)["code"] == "tools_stale"
    assert tiles.ha_mcp(None, NOW, 10)["state"] == "bad"


def test_bridge_down_when_the_status_file_is_too_old_or_not_connected():
    fresh = {"ts": NOW - 31, "state": "connected", "announced": True, "tools_source": "live"}
    assert tiles.bridge(fresh, NOW)["state"] == "ok"
    assert tiles.bridge({**fresh, "ts": NOW - 64.9}, NOW)["state"] == "ok"
    old = tiles.bridge({**fresh, "ts": NOW - 65.1}, NOW)
    assert old["state"] == "bad" and old["code"] == "stale"
    stopped = tiles.bridge({**fresh, "state": "stopped", "ts": NOW - 1}, NOW)
    assert stopped["state"] == "bad" and stopped["code"] == "stopped"
    assert tiles.bridge({**fresh, "tools_source": "cache"}, NOW)["code"] == "cached_tools"
    assert tiles.bridge(None, NOW, "no_status_file")["state"] == "bad"


def test_endpoint_and_tunnel():
    assert tiles.endpoint(0, 3, None, 1)["state"] == "ok"
    assert tiles.endpoint(1, 3, "timeout", 1)["state"] == "ok"
    assert tiles.endpoint(2, None, "ConnectionRefusedError", 4)["state"] == "bad"
    assert tiles.tunnel(False, None, NOW, 10)["state"] == "off"
    assert tiles.tunnel(True, {"ok": True, "ts": NOW - 5}, NOW, 10)["state"] == "ok"
    assert tiles.tunnel(True, {"ok": False, "ts": NOW - 5}, NOW, 10)["state"] == "bad"
    assert tiles.tunnel(True, None, NOW, 10)["state"] == "bad"
