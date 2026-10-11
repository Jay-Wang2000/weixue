"""ha-mcp read timeout per request: a held call outlasts its policy wait and yields an owned, approvable token;
the ADR-9 budget answers pending first; a call that never answers ends unknown after the read timeout."""
import asyncio
import dataclasses
import socket
import time

import httpx
import pytest
import pytest_asyncio
import uvicorn

from fake_hamcp import FakeHaMcp
from weixue_agent import ha_adapter
from weixue_agent.config import HA_MCP_READ_TIMEOUT_S, ConfigError, load_settings
from weixue_agent.store import Store
from weixue_hamcp import CONNECT_TIMEOUT_S, HaMcpClient

TOKEN = "t" * 40
PLUG = {"domain": "switch", "service": "turn_on", "entity_id": "switch.working_desk_plug"}
LIGHT = {"domain": "light", "service": "turn_on", "entity_id": "light.living_room_ceiling_lights"}
WAITER = {"device_id": "sim-device-01", "chat_id": "cht_1", "task_id": "tsk_1", "turn_id": "trn_1",
          "tool_use_id": "tu_1"}


def settings_for(tmp_path, url, **env):
    return load_settings({"AGENT_SERVICE_TOKEN": TOKEN, "HA_MCP_URL": url, "AGENT_DATA_DIR": str(tmp_path),
                          "AGENT_MCP_ENDPOINT_HOST": "", **env})


class Recorder:
    """ApprovalListener that records (callback, state, ha_token, monotonic time)."""

    def __init__(self):
        self.seen = []

    def _add(self, kind, approval):
        self.seen.append((kind, approval["state"], approval["ha_token"], time.monotonic()))

    def on_pending(self, approval, waiter):
        self._add("pending", approval)

    def on_correlated(self, approval):
        self._add("correlated", approval)

    def on_terminal(self, approval):
        self._add("terminal", approval)


class Capturing(httpx.AsyncBaseTransport):
    """Records the timeout extension of every request it forwards."""

    def __init__(self, inner):
        self.inner = inner
        self.timeouts = []

    async def handle_async_request(self, request):
        self.timeouts.append((request.url.path, request.extensions.get("timeout")))
        return await self.inner.handle_async_request(request)


@pytest_asyncio.fixture
async def tcp_ha():
    """Serves a FakeHaMcp over a real TCP socket on 127.0.0.1 (httpx read timeouts apply)."""
    running = []

    async def _start(fake):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(16)
        server = uvicorn.Server(uvicorn.Config(fake.app(), log_level="warning", lifespan="off",
                                               timeout_graceful_shutdown=1))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            await asyncio.sleep(0.01)
        running.append((fake, server, task))
        return f"http://127.0.0.1:{sock.getsockname()[1]}/private_test"

    yield _start
    for fake, server, task in running:
        fake.elapse_wait()
        server.should_exit = True
        await asyncio.wait_for(task, 10)


async def gateway_on(settings, client):
    gateway = ha_adapter.HaGateway(settings, Store(settings.db_path), client)
    gateway.listener = Recorder()
    await gateway.refresh()
    return gateway


async def wait_state(gateway, approval_id, states, timeout):
    deadline = time.monotonic() + timeout
    while gateway.store.approval(approval_id)["state"] not in states:
        if time.monotonic() > deadline:
            raise AssertionError(f"{approval_id} is {gateway.store.approval(approval_id)['state']}, wanted {states}")
        await asyncio.sleep(0.05)
    return gateway.store.approval(approval_id)


def test_read_timeout_setting():
    base = {"AGENT_SERVICE_TOKEN": TOKEN, "HA_MCP_URL": "http://ha.test:9584/private_test"}
    assert load_settings(base).ha_mcp_read_timeout_s == HA_MCP_READ_TIMEOUT_S == 35
    assert load_settings({**base, "HA_MCP_READ_TIMEOUT_S": ""}).ha_mcp_read_timeout_s == 35
    assert load_settings({**base, "HA_MCP_READ_TIMEOUT_S": "50"}).ha_mcp_read_timeout_s == 50
    for bad, message in (("8", "must exceed"), ("-1", "must exceed"), ("soon", "must be a number")):
        with pytest.raises(ConfigError, match=message):
            load_settings({**base, "HA_MCP_READ_TIMEOUT_S": bad})


async def test_tool_calls_carry_the_configured_read_timeout_and_a_short_connect_timeout(tmp_path):
    fake = FakeHaMcp(wait_seconds=20)
    transport = Capturing(httpx.ASGITransport(app=fake.app()))
    settings = settings_for(tmp_path, "http://ha.test:9584/private_test", HA_MCP_READ_TIMEOUT_S="42")
    gateway = await gateway_on(settings, HaMcpClient(settings.ha_mcp_url, transport=transport))
    transport.timeouts.clear()
    await gateway.call(dict(WAITER), "ha_call_service", LIGHT)
    await gateway.ping()
    path = "/private_test"
    rpc = [t for p, t in transport.timeouts if p == path]
    assert rpc[0] == {"connect": CONNECT_TIMEOUT_S, "read": 42, "write": 42, "pool": 42}
    assert rpc[1]["read"] == 10 and rpc[1]["connect"] == CONNECT_TIMEOUT_S
    await gateway.stop()
    gateway.store.close()


async def test_policy_wait_at_or_above_the_read_timeout_is_logged(tmp_path, capsys):
    fake = FakeHaMcp(wait_seconds=40)
    settings = settings_for(tmp_path, "http://ha.test:9584/private_test")
    gateway = await gateway_on(settings, HaMcpClient(settings.ha_mcp_url,
                                                     transport=httpx.ASGITransport(app=fake.app())))
    assert "read_timeout_s=35 wait_seconds=40" in capsys.readouterr().out
    fake.wait_seconds = 20
    await gateway.refresh()
    assert "ha_timeout_config" not in capsys.readouterr().out
    await gateway.stop()
    gateway.store.close()


async def test_held_call_answers_pending_at_8_s_and_becomes_approvable_after_the_20_s_hold(
        tmp_path, tcp_ha, monkeypatch):
    monkeypatch.setattr(ha_adapter, "HA_CALL_BUDGET_S", 8)
    fake = FakeHaMcp(wait_seconds=20)
    settings = settings_for(tmp_path, await tcp_ha(fake))
    gateway = await gateway_on(settings, HaMcpClient(settings.ha_mcp_url))
    started = time.monotonic()
    result = await gateway.call(dict(WAITER), "ha_call_service", PLUG)
    answered = time.monotonic() - started
    assert 7.9 <= answered < 9.5
    assert result.content[0].text.startswith("PENDING_APPROVAL id=apr_")
    approval_id = result.content[0].text.split()[1].removeprefix("id=").rstrip(":")
    assert gateway.store.approval(approval_id)["state"] == "pending"
    row = await wait_state(gateway, approval_id, ("awaiting_decision_after_wait",), timeout=20)
    owned_after = time.monotonic() - started
    assert 19.5 <= owned_after < 25
    assert row["token_source"] == "response" and row["ha_token"] == next(iter(fake.queue))
    kinds = [(k, s) for k, s, _, _ in gateway.listener.seen]
    assert kinds == [("pending", "pending"), ("correlated", "awaiting_decision_after_wait")]
    await gateway.decide(approval_id, "approve", "device", WAITER["device_id"])
    row = await wait_state(gateway, approval_id, ("executed", "failed", "unknown", "denied"), timeout=10)
    assert row["state"] == "executed"
    assert fake.decisions == [{"token": row["ha_token"], "decision": "approved"}]
    assert [c["tool"] for c in fake.calls] == ["ha_call_service"] * 2 and len(fake.executed) == 1
    await gateway.stop()
    gateway.store.close()


async def test_call_that_never_answers_ends_unknown_after_the_read_timeout_and_is_not_retried(tmp_path, tcp_ha):
    fake = FakeHaMcp(wait_seconds=3600)
    settings = dataclasses.replace(settings_for(tmp_path, await tcp_ha(fake)), ha_mcp_read_timeout_s=1.5)
    gateway = await gateway_on(settings, HaMcpClient(settings.ha_mcp_url))
    started = time.monotonic()
    result = await gateway.call(dict(WAITER), "ha_call_service", PLUG)
    assert result.content[0].text.startswith("PENDING_APPROVAL id=apr_")
    approval_id = result.content[0].text.split()[1].removeprefix("id=").rstrip(":")
    row = await wait_state(gateway, approval_id, ha_adapter.TERMINAL_STATES, timeout=5)
    assert 1.4 <= time.monotonic() - started < 3
    assert row["state"] == "unknown" and row["result_text"] == "result unknown (ReadTimeout)"
    assert [(k, s) for k, s, _, _ in gateway.listener.seen] == [("pending", "pending"), ("terminal", "unknown")]
    await asyncio.sleep(1.0)
    assert len(fake.calls) == 1 and fake.decisions == [] and fake.executed == []
    again = await gateway.call(dict(WAITER, tool_use_id="tu_2"), "ha_call_service", PLUG)
    assert again.is_error and again.content[0].text == ha_adapter.BLOCKED_TEXT
    assert len(fake.calls) == 1
    await gateway.stop()
    gateway.store.close()
