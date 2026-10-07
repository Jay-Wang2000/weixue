"""Unit tests for server/mcp-bridge: config parsing, voice filter, ha-mcp client, relay."""
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

BRIDGE_DIR = Path(__file__).resolve().parents[1] / "mcp-bridge"
FILTER_FILE = Path(__file__).resolve().parents[1] / "policy" / "voice_bridge.json"
PROPOSED_FILTER_FILE = FILTER_FILE.with_name("voice_bridge.proposed.json")
sys.path.insert(0, str(BRIDGE_DIR))

from weixue_bridge import bridge as bridge_mod  # noqa: E402
from weixue_bridge.config import (  # noqa: E402
    ConfigError,
    VoiceFilter,
    load_config,
    load_voice_filter,
    parse_endpoint_url,
    parse_ha_mcp_url,
    redact,
)
from weixue_bridge.hamcp import HaMcpClient, HaMcpError, parse_sse  # noqa: E402
from weixue_bridge.health import healthy  # noqa: E402
from weixue_bridge.voice_filter import filter_tools, refusal  # noqa: E402

SECRET = "private_s3cr3tS3cr3tS3cr3t"
HA_URL = f"http://192.168.8.142:9584/{SECRET}"
ENDPOINT = "ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=abc%3D%3D"
VOICE = VoiceFilter(
    tools=frozenset({"ha_get_state", "ha_call_service"}),
    call_service_domains=frozenset({"light", "switch"}),
)


def write_filter(tmp_path, data):
    path = tmp_path / "voice_bridge.json"
    path.write_text(json.dumps(data))
    return str(path)


# --- config -----------------------------------------------------------------


def test_ha_mcp_url_strips_trailing_slash():
    assert parse_ha_mcp_url(f" {HA_URL}/ \n") == HA_URL


@pytest.mark.parametrize(
    "raw",
    ["http://192.168.8.142:9584", "http://192.168.8.142:9584/", f"ws://h:9584/{SECRET}", f"{HA_URL}?x=1", "nonsense"],
)
def test_ha_mcp_url_rejects_without_echoing(raw):
    with pytest.raises(ConfigError) as e:
        parse_ha_mcp_url(raw)
    assert SECRET not in str(e.value)


def test_endpoint_url_accepts_tool_side_path():
    assert parse_endpoint_url(ENDPOINT + "\n") == ENDPOINT


@pytest.mark.parametrize(
    "raw",
    [
        "ws://mcp-endpoint-server:8004/mcp_endpoint/call/?token=abc",
        "ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/",
        "ws://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=",
        "http://mcp-endpoint-server:8004/mcp_endpoint/mcp/?token=abc",
    ],
)
def test_endpoint_url_rejects(raw):
    with pytest.raises(ConfigError) as e:
        parse_endpoint_url(raw)
    assert "abc" not in str(e.value)


def test_redact_keeps_only_scheme_host_port():
    assert redact(HA_URL) == "http://192.168.8.142:9584"
    assert redact(ENDPOINT) == "ws://mcp-endpoint-server:8004"


def test_load_config_prefers_env_endpoint(tmp_path):
    endpoint_file = tmp_path / "endpoint-url"
    endpoint_file.write_text("ws://other:8004/mcp_endpoint/mcp/?token=zzz\n")
    config = load_config(
        {
            "HA_MCP_URL": HA_URL,
            "MCP_ENDPOINT": ENDPOINT,
            "MCP_ENDPOINT_FILE": str(endpoint_file),
            "VOICE_FILTER_FILE": str(FILTER_FILE),
            "BRIDGE_HEALTH_FILE": str(tmp_path / "h"),
        }
    )
    assert config.endpoint_url == ENDPOINT
    assert config.ha_mcp_url == HA_URL
    assert config.health_file == str(tmp_path / "h")


def test_load_config_reads_endpoint_file(tmp_path):
    endpoint_file = tmp_path / "endpoint-url"
    endpoint_file.write_text(ENDPOINT + "\n")
    config = load_config(
        {"HA_MCP_URL": HA_URL, "MCP_ENDPOINT_FILE": str(endpoint_file), "VOICE_FILTER_FILE": str(FILTER_FILE)}
    )
    assert config.endpoint_url == ENDPOINT


def test_load_config_requires_ha_mcp_url(tmp_path):
    with pytest.raises(ConfigError, match="HA_MCP_URL is not set"):
        load_config({"HA_MCP_URL": "  ", "MCP_ENDPOINT": ENDPOINT, "VOICE_FILTER_FILE": str(FILTER_FILE)})


def test_load_config_missing_endpoint_file(tmp_path):
    with pytest.raises(ConfigError, match="unreadable"):
        load_config(
            {
                "HA_MCP_URL": HA_URL,
                "MCP_ENDPOINT_FILE": str(tmp_path / "missing"),
                "VOICE_FILTER_FILE": str(FILTER_FILE),
            }
        )


@pytest.mark.parametrize(
    "path, domains",
    [
        (FILTER_FILE, {"light"}),
        (PROPOSED_FILTER_FILE, {"light", "switch", "cover", "media_player", "climate"}),
    ],
)
def test_repo_voice_filters(path, domains):
    voice = load_voice_filter(str(path))
    assert voice.tools == load_voice_filter(str(FILTER_FILE)).tools
    assert "ha_call_service" in voice.tools
    assert all(name.startswith("ha_") for name in voice.tools)
    assert not voice.tools & {"ha_config_set_automation", "ha_bulk_control", "ha_restart"}
    assert voice.call_service_domains == domains
    assert not voice.call_service_domains & {
        "lock",
        "alarm_control_panel",
        "hassio",
        "shell_command",
        "python_script",
        "homeassistant",
    }


def test_voice_filter_lowercases_domains(tmp_path):
    voice = load_voice_filter(write_filter(tmp_path, {"tools": ["ha_call_service"], "call_service_domains": ["Light"]}))
    assert voice.call_service_domains == frozenset({"light"})


@pytest.mark.parametrize(
    "data",
    [
        {"tools": [], "call_service_domains": []},
        {"tools": ["ha_get_state"]},
        {"tools": ["ha_get_state"], "call_service_domains": [], "extra": 1},
        {"tools": "ha_get_state", "call_service_domains": []},
        {"tools": [""], "call_service_domains": []},
        [],
    ],
)
def test_voice_filter_rejects(tmp_path, data):
    with pytest.raises(ConfigError):
        load_voice_filter(write_filter(tmp_path, data))


def test_voice_filter_rejects_bad_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{")
    with pytest.raises(ConfigError, match="JSONDecodeError"):
        load_voice_filter(str(path))


# --- voice filter -------------------------------------------------------------


def test_filter_tools_keeps_allowlisted():
    tools = [{"name": "ha_get_state"}, {"name": "ha_restart"}, "junk", {"name": "ha_call_service"}]
    assert [t["name"] for t in filter_tools(tools, VOICE)] == ["ha_get_state", "ha_call_service"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("ha_get_state", {"entity_id": "light.x"}),
        ("ha_call_service", {"domain": "light", "service": "turn_off", "entity_id": "light.x"}),
        ("ha_call_service", {"domain": "Switch", "service": "turn_on", "entity_id": "switch.x"}),
        ("ha_call_service", {"domain": "light", "service": "turn_on", "ws_command": None}),
    ],
)
def test_refusal_allows(name, args):
    assert refusal(VOICE, name, args) is None


@pytest.mark.parametrize(
    "name,args,needle",
    [
        ("ha_restart", {}, "not enabled"),
        ("ha_call_service", {"domain": "lock", "service": "unlock"}, "'lock' services"),
        ("ha_call_service", {"domain": "homeassistant", "service": "restart"}, "'homeassistant'"),
        ("ha_call_service", {"domain": "hassio", "service": "addon_stop"}, "'hassio'"),
        ("ha_call_service", {"service": "turn_on"}, "None"),
        ("ha_call_service", {"ws_command": "config/auth/list", "domain": "light"}, "WebSocket"),
        ("ha_call_service", "not-a-dict", "object"),
    ],
)
def test_refusal_refuses(name, args, needle):
    reason = refusal(VOICE, name, args)
    assert reason and needle in reason


# --- ha-mcp client ------------------------------------------------------------


def test_parse_sse_events():
    text = (
        'event: message\r\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\r\n\r\n'
        'event: message\ndata: {"jsonrpc":"2.0",\ndata: "id":3,"result":{}}\n\n'
        "data: not json\n\n"
    )
    assert parse_sse(text) == [
        {"jsonrpc": "2.0", "method": "notifications/progress"},
        {"jsonrpc": "2.0", "id": 3, "result": {}},
    ]


def sse(*messages):
    body = "".join(f"event: message\r\ndata: {json.dumps(m)}\r\n\r\n" for m in messages)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


async def test_client_returns_matching_response_and_tracks_protocol():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        if body.get("method") == "initialize":
            return sse({"jsonrpc": "2.0", "id": body["id"], "result": {"protocolVersion": "2024-11-05"}})
        if "id" not in body:
            return httpx.Response(202)
        return sse(
            {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
            {"jsonrpc": "2.0", "id": body["id"], "result": {"ok": True}},
        )

    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(handler))
    init = await client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"] == "2024-11-05"
    assert await client.send({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    resp = await client.send({"jsonrpc": "2.0", "id": "a_7", "method": "tools/call", "params": {}})
    assert resp == {"jsonrpc": "2.0", "id": "a_7", "result": {"ok": True}}
    assert "mcp-protocol-version" not in seen[0].headers
    assert seen[2].headers["mcp-protocol-version"] == "2024-11-05"
    assert "text/event-stream" in seen[2].headers["accept"]
    await client.aclose()


async def test_client_echoes_session_id_and_accepts_json():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, headers={"mcp-session-id": "sess-1"}, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(handler))
    await client.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    await client.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert "mcp-session-id" not in seen[0].headers
    assert seen[1].headers["mcp-session-id"] == "sess-1"
    await client.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500, text="boom"),
        httpx.Response(200, headers={"content-type": "text/event-stream"}, text=""),
        httpx.Response(200, headers={"content-type": "application/json"}, text="<html>"),
    ],
)
async def test_client_errors_never_contain_url(response):
    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(HaMcpError) as e:
        await client.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert SECRET not in str(e.value)
    await client.aclose()


async def test_client_transport_error_never_contains_url():
    def handler(request):
        raise httpx.ConnectError(f"cannot reach {request.url}")

    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(handler))
    with pytest.raises(HaMcpError) as e:
        await client.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert str(e.value) == "ConnectError"
    await client.aclose()


# --- relay --------------------------------------------------------------------


class FakeHa:
    def __init__(self, responses=None, error=None, rpc_errors=None):
        self.sent = []
        self.responses = responses or {}
        self.rpc_errors = rpc_errors or {}
        self.error = error

    async def send(self, message):
        self.sent.append(message)
        if self.error:
            raise self.error
        if "id" not in message:
            return None
        if message["method"] in self.rpc_errors:
            return {"jsonrpc": "2.0", "id": message["id"], "error": self.rpc_errors[message["method"]]}
        return {"jsonrpc": "2.0", "id": message["id"], "result": self.responses.get(message["method"], {})}


def make_bridge(ha):
    out = []

    async def send(raw):
        out.append(json.loads(raw))

    return bridge_mod.Bridge(VOICE, ha, send), out


TOOLS = {"tools": [{"name": "ha_get_state"}, {"name": "ha_config_set_automation"}, {"name": "ha_call_service"}]}


async def test_tools_list_is_filtered():
    bridge, out = make_bridge(FakeHa({"tools/list": TOOLS}))
    await bridge.handle(json.dumps({"jsonrpc": "2.0", "id": "u_2", "method": "tools/list"}))
    assert out == [
        {"jsonrpc": "2.0", "id": "u_2", "result": {"tools": [{"name": "ha_get_state"}, {"name": "ha_call_service"}]}}
    ]


async def test_refused_call_is_not_forwarded():
    ha = FakeHa()
    bridge, out = make_bridge(ha)
    await bridge.handle(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "u_5",
                "method": "tools/call",
                "params": {"name": "ha_call_service", "arguments": {"domain": "lock", "service": "unlock"}},
            }
        )
    )
    assert ha.sent == []
    assert out[0]["id"] == "u_5"
    assert out[0]["result"]["isError"] is False
    assert "'lock' services" in out[0]["result"]["content"][0]["text"]


async def test_allowed_call_is_forwarded_unchanged():
    ha = FakeHa({"tools/call": {"content": [{"type": "text", "text": "done"}]}})
    bridge, out = make_bridge(ha)
    message = {
        "jsonrpc": "2.0",
        "id": "u_6",
        "method": "tools/call",
        "params": {"name": "ha_call_service", "arguments": {"domain": "light", "service": "turn_on"}},
    }
    await bridge.handle(json.dumps(message))
    assert ha.sent == [message]
    assert out == [{"jsonrpc": "2.0", "id": "u_6", "result": {"content": [{"type": "text", "text": "done"}]}}]


ENTITY_NOT_FOUND = json.dumps({"success": False, "error": {"code": "ENTITY_NOT_FOUND", "message": "light.x not found"}})


@pytest.mark.parametrize(
    "response, text",
    [
        (
            {"id": 4, "result": {"content": [{"type": "text", "text": ENTITY_NOT_FOUND}], "isError": True}},
            "Tool error: " + ENTITY_NOT_FOUND,
        ),
        (
            {"id": 4, "result": {"content": [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}], "isError": True}},
            "Tool error: a\nb",
        ),
        ({"id": 4, "result": {"content": [], "isError": True}}, "Tool error: no details"),
        ({"id": 4, "error": {"code": -32602, "message": "Unknown tool: ha_x"}}, "Tool error: Unknown tool: ha_x"),
        ({"id": 4, "error": "boom"}, "Tool error: unknown error"),
    ],
)
def test_for_llm_rewrites_failures_as_text(response, text):
    assert bridge_mod.for_llm(response) == {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def test_for_llm_keeps_successes():
    ok = {"jsonrpc": "2.0", "id": 4, "result": {"content": [{"type": "text", "text": "on"}], "isError": False}}
    assert bridge_mod.for_llm(ok) is ok


async def test_ha_mcp_error_result_is_forwarded_as_text():
    ha = FakeHa({"tools/call": {"content": [{"type": "text", "text": ENTITY_NOT_FOUND}], "isError": True}})
    bridge, out = make_bridge(ha)
    await bridge.handle(
        json.dumps({"jsonrpc": "2.0", "id": "u_9", "method": "tools/call", "params": {"name": "ha_get_state"}})
    )
    assert out[0]["id"] == "u_9"
    assert out[0]["result"] == {"content": [{"type": "text", "text": "Tool error: " + ENTITY_NOT_FOUND}], "isError": False}


async def test_ha_mcp_rpc_error_on_call_is_forwarded_as_text():
    ha = FakeHa(rpc_errors={"tools/call": {"code": -32602, "message": "bad arguments"}})
    bridge, out = make_bridge(ha)
    await bridge.handle(
        json.dumps({"jsonrpc": "2.0", "id": "u_10", "method": "tools/call", "params": {"name": "ha_get_state"}})
    )
    assert out[0]["result"]["content"][0]["text"] == "Tool error: bad arguments"


class BridgeSocket:
    """Server-side endpoint socket wired straight to a Bridge, replies fed to the server's handler."""

    def __init__(self, ha, handler):
        self.client = None
        self._handler = handler
        self._bridge = bridge_mod.Bridge(VOICE, ha, self._reply)

    async def _reply(self, raw):
        await self._handler(self.client, raw)

    async def send(self, raw):
        await self._bridge.handle(raw)


async def _server_executes(ha, tool, arguments):
    from types import SimpleNamespace

    from core.providers.tools.mcp_endpoint.mcp_endpoint_client import MCPEndpointClient
    from core.providers.tools.mcp_endpoint.mcp_endpoint_executor import MCPEndpointExecutor
    from core.providers.tools.mcp_endpoint.mcp_endpoint_handler import handle_mcp_endpoint_message

    client = MCPEndpointClient()
    socket = BridgeSocket(ha, handle_mcp_endpoint_message)
    socket.client = client
    client.set_websocket(socket)
    await client.add_tool({"name": tool, "description": "", "inputSchema": {"type": "object"}})
    await client.set_ready(True)
    conn = SimpleNamespace(mcp_endpoint_client=client)
    return await MCPEndpointExecutor(conn).execute(conn, tool, arguments)


@pytest.mark.parametrize(
    "ha, tool, arguments, needle",
    [
        (FakeHa(), "ha_call_service", {"domain": "lock", "service": "unlock"}, "'lock' services are disabled"),
        (
            FakeHa({"tools/call": {"content": [{"type": "text", "text": ENTITY_NOT_FOUND}], "isError": True}}),
            "ha_get_state",
            {"entity_id": "light.x"},
            "ENTITY_NOT_FOUND",
        ),
        (FakeHa(error=HaMcpError("ConnectError")), "ha_get_state", {}, "Home Assistant is unreachable right now."),
    ],
)
async def test_server_hands_failure_text_to_llm(ha, tool, arguments, needle):
    from plugins_func.register import Action

    response = await _server_executes(ha, tool, arguments)
    assert response.action == Action.REQLLM
    assert needle in response.result


async def test_notification_is_forwarded_without_reply():
    ha = FakeHa()
    bridge, out = make_bridge(ha)
    await bridge.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))
    assert ha.sent[0]["method"] == "notifications/initialized"
    assert out == []


async def test_ha_failure_replies_per_method():
    bridge, out = make_bridge(FakeHa(error=HaMcpError("ConnectError")))
    await bridge.handle(
        json.dumps({"jsonrpc": "2.0", "id": "u_7", "method": "tools/call", "params": {"name": "ha_get_state"}})
    )
    await bridge.handle(json.dumps({"jsonrpc": "2.0", "id": "u_8", "method": "tools/list"}))
    await bridge.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert out[0]["result"]["content"][0]["text"] == "Home Assistant is unreachable right now."
    assert out[1]["error"]["message"] == "ha-mcp unavailable (ConnectError)"
    assert len(out) == 2


@pytest.mark.parametrize("raw", ["not json", "[]", json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}})])
async def test_non_requests_are_dropped(raw):
    ha = FakeHa()
    bridge, out = make_bridge(ha)
    await bridge.handle(raw)
    assert ha.sent == [] and out == []


async def test_announce_registers_filtered_tools():
    ha = FakeHa({"initialize": {"protocolVersion": "2024-11-05", "serverInfo": {"name": "ha-mcp"}}, "tools/list": TOOLS})
    bridge, out = make_bridge(ha)
    assert await bridge.announce() == 2
    assert [m["method"] for m in ha.sent] == ["initialize", "notifications/initialized", "tools/list"]
    assert out[0]["id"] == bridge_mod.ANNOUNCE_INIT_ID
    assert out[1]["id"] == bridge_mod.ANNOUNCE_TOOLS_ID
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["ha_get_state", "ha_call_service"]


# --- health -------------------------------------------------------------------


def test_health_file_freshness(tmp_path):
    path = tmp_path / "healthy"
    assert not healthy(str(path), time.time())
    path.write_text("1")
    assert healthy(str(path), time.time())
    old = time.time() - 600
    os.utime(path, (old, old))
    assert not healthy(str(path), time.time())


# --- reconnect ----------------------------------------------------------------


class FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class StopLoop(Exception):
    pass


@pytest.fixture
def reconnect(monkeypatch, tmp_path):
    """Runs bridge.run against a scripted connect: each entry is an exception to raise or None to connect."""
    state = {
        "script": [],
        "default": OSError("unreachable"),
        "connects": 0,
        "sleeps": [],
        "sleep_limit": None,
        "interfaces": [{"lo"}],
        "ha": FakeHa({"tools/list": TOOLS}),
    }

    def connect(url, **kwargs):
        state["connects"] += 1
        step = state["script"].pop(0) if state["script"] else state["default"]
        if step is not None:
            raise step
        return FakeWs()

    async def sleep(seconds):
        if state["sleep_limit"] is not None and len(state["sleeps"]) >= state["sleep_limit"]:
            raise StopLoop
        state["sleeps"].append(seconds)

    def interfaces():
        return state["interfaces"].pop(0) if len(state["interfaces"]) > 1 else state["interfaces"][0]

    monkeypatch.setattr(bridge_mod.websockets, "connect", connect)
    monkeypatch.setattr(bridge_mod.asyncio, "sleep", sleep)
    monkeypatch.setattr(bridge_mod, "_interface_names", interfaces)
    config = bridge_mod.BridgeConfig(HA_URL, ENDPOINT, VOICE, str(tmp_path / "healthy"))

    async def run():
        with pytest.raises(bridge_mod.BridgeExit) as e:
            await bridge_mod.run(config, state["ha"])
        return str(e.value)

    state["config"] = config
    state["run"] = run
    return state


async def test_bridge_exits_after_max_consecutive_endpoint_failures(reconnect):
    message = await reconnect["run"]()
    assert reconnect["connects"] == bridge_mod.ENDPOINT_MAX_FAILURES
    assert message == f"endpoint unreachable for {bridge_mod.ENDPOINT_MAX_FAILURES} consecutive attempts"
    assert len(reconnect["sleeps"]) == bridge_mod.ENDPOINT_MAX_FAILURES - 1
    expected = sum(min(2**i, bridge_mod.RECONNECT_MAX_S) for i in range(bridge_mod.ENDPOINT_MAX_FAILURES - 1))
    assert sum(reconnect["sleeps"]) == expected


async def test_bridge_connect_resets_failure_count(reconnect):
    first = bridge_mod.ENDPOINT_MAX_FAILURES - 1
    reconnect["script"] = [OSError("unreachable")] * first + [None]
    await reconnect["run"]()
    assert reconnect["connects"] == first + 1 + bridge_mod.ENDPOINT_MAX_FAILURES


async def test_bridge_exits_when_namespace_interface_is_gone(reconnect):
    reconnect["interfaces"] = [{"lo", "eth0"}, {"lo"}]
    assert await reconnect["run"]() == "eth0 is gone from the network namespace"
    assert reconnect["connects"] == 1


async def test_bridge_ignores_interface_absent_at_start(reconnect):
    reconnect["interfaces"] = [{"lo"}, {"lo", "eth0"}, {"lo"}]
    await reconnect["run"]()
    assert reconnect["connects"] == bridge_mod.ENDPOINT_MAX_FAILURES


async def test_bridge_keeps_running_while_only_ha_mcp_fails(reconnect):
    iterations = 2 * bridge_mod.ENDPOINT_MAX_FAILURES
    reconnect["default"] = None
    reconnect["ha"] = FakeHa(error=HaMcpError("ConnectError"))
    reconnect["sleep_limit"] = iterations
    with pytest.raises(StopLoop):
        await bridge_mod.run(reconnect["config"], reconnect["ha"])
    assert reconnect["connects"] == iterations + 1
