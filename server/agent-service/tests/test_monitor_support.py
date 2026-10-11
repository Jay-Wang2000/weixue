"""Monitor support: MONITOR_TOKEN read scope, probe cadence and timeout, sparse health_probes rows."""
import asyncio
import dataclasses

import httpx
import pytest

from conftest import TOKEN
from weixue_agent import config
from weixue_agent.api import Api
from weixue_agent.config import ConfigError, load_settings
from weixue_agent.monitor import Monitor

MONITOR = "m" * 40
BASE_ENV = {"AGENT_SERVICE_TOKEN": TOKEN, "AGENT_DATA_DIR": "/tmp/x"}


def _client(h, token):
    h.service.settings = dataclasses.replace(h.service.settings, monitor_token=MONITOR)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=Api(h.service).app()), base_url="http://agent",
                             headers={"Authorization": f"Bearer {token}"}, timeout=30)


async def test_monitor_token_reads_status_and_trace_only(make):
    h = await make()
    _, events = await h.send("hello")
    task_id = events[0]["task_id"]
    await h.wait_task(task_id, "done")
    monitor = _client(h, MONITOR)
    try:
        assert (await monitor.get("/v1/status")).status_code == 200
        assert (await monitor.get(f"/v1/tasks/{task_id}/trace")).status_code == 200
        chat_id = h.store.task(task_id)["chat_id"]
        denied = [
            ("GET", "/v1/chats?device_id=sim-device-01"), ("POST", "/v1/chats"), ("GET", f"/v1/chats/{chat_id}"),
            ("GET", f"/v1/tasks/{task_id}"), ("POST", f"/v1/chats/{chat_id}/messages"),
            ("POST", f"/v1/chats/{chat_id}/interrupt"), ("POST", f"/v1/chats/{chat_id}/approve"),
            ("POST", f"/v1/chats/{chat_id}/deny"), ("POST", f"/v1/chats/{chat_id}/answer"),
            ("POST", f"/v1/chats/{chat_id}/read"), ("GET", "/v1/deliveries/stream?device_id=sim-device-01"),
            ("POST", "/v1/deliveries/dlv_x/ack"), ("GET", "/v1/devices/sim-device-01/snapshot"),
            ("POST", "/v1/devices/sim-device-01/connection"), ("GET", f"/v1/chats/{chat_id}/events"),
        ]
        for method, path in denied:
            resp = await monitor.request(method, path, json={} if method == "POST" else None)
            assert resp.status_code == 401, (method, path, resp.status_code)
        service = _client(h, TOKEN)
        assert (await service.get(f"/v1/tasks/{task_id}")).status_code == 200
        await service.aclose()
    finally:
        await monitor.aclose()


async def test_monitor_token_is_refused_while_unset(make):
    h = await make()
    anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=Api(h.service).app()), base_url="http://agent",
                             headers={"Authorization": f"Bearer {MONITOR}"})
    assert (await anon.get("/v1/status")).status_code == 401
    await anon.aclose()


@pytest.mark.parametrize("value", ["short", TOKEN])
def test_monitor_token_must_be_long_and_distinct(value):
    with pytest.raises(ConfigError) as e:
        load_settings({**BASE_ENV, "MONITOR_TOKEN": value})
    assert value not in str(e.value)


def test_probe_cadence_defaults_and_overrides():
    settings = load_settings(BASE_ENV)
    assert (settings.probe_s, settings.probe_timeout_s) == (10, 5)
    assert str(settings.db_path) == "/tmp/x/db/agent.db"
    settings = load_settings({**BASE_ENV, "AGENT_HA_PROBE_S": "30", "AGENT_HA_PROBE_TIMEOUT_S": "10"})
    assert (settings.probe_s, settings.probe_timeout_s) == (30, 10)
    with pytest.raises(ConfigError):
        load_settings({**BASE_ENV, "AGENT_HA_PROBE_S": "5", "AGENT_HA_PROBE_TIMEOUT_S": "5"})


async def test_ping_is_bounded_by_the_probe_timeout(make):
    h = await make()

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(5)

    h.service.ha.client.request = slow
    started = asyncio.get_running_loop().time()
    assert await h.service.ha.ping(timeout_s=0.1) is False
    assert asyncio.get_running_loop().time() - started < 1
    assert "timed out" in h.service.ha.last_error


async def test_probe_rows_only_on_change_or_every_300_s(make):
    h = await make()
    now = [1000.0]
    monitor = Monitor(h.store, h.service.ha, h.engine, h.service.outbox, None, clock=lambda: now[0])
    h.store.execute("DELETE FROM health_probes")

    def rows():
        return [(r["ok"]) for r in h.store.all("SELECT ok FROM health_probes WHERE probe = 'ha_mcp' ORDER BY id")]

    await monitor.probe_all()
    now[0] += 10
    await monitor.probe_all()
    assert rows() == [1]
    assert monitor.latest["ha_mcp"]["ok"] is True and monitor.latest["ha_mcp"]["tool_count"] > 0
    h.fake_ha.down = True
    now[0] += 10
    await monitor.probe_all()
    now[0] += 10
    await monitor.probe_all()
    assert rows() == [1, 0]
    now[0] += config.PROBE_ROW_EVERY_S
    await monitor.probe_all()
    assert rows() == [1, 0, 0]
    h.fake_ha.down = False
    now[0] += 10
    await monitor.probe_all()
    assert rows() == [1, 0, 0, 1]
    assert monitor.latest["ha_mcp"]["ok"] is True
