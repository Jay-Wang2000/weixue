"""Unit tests for server/mcp-bridge: config parsing, voice filter, ha-mcp client, relay."""
import asyncio
import itertools
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
from weixue_bridge import catalog as catalog_mod  # noqa: E402
from weixue_bridge import hamcp as hamcp_mod  # noqa: E402
from weixue_bridge.catalog import ToolCatalog  # noqa: E402
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


async def test_client_echoes_session_id_from_initialize_and_accepts_json():
    seen = []

    def handler(request):
        seen.append(request)
        body = json.loads(request.content)
        sid = "sess-1" if body["method"] == "initialize" else "sess-other"
        return httpx.Response(200, headers={"mcp-session-id": sid}, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(handler))
    await client.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    await client.send({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    await client.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert "mcp-session-id" not in seen[0].headers
    assert seen[1].headers["mcp-session-id"] == "sess-1"
    assert seen[2].headers["mcp-session-id"] == "sess-1"
    await client._client.aclose()


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
        self.session_id = None
        self.sessions_opened = 0
        self.init_response = None
        self.timings = {}
        self.closed = False

    async def ensure_session(self):
        if self.init_response is None:
            self.init_response = await self.send({"jsonrpc": "2.0", "id": "init", "method": "initialize"})
            self.sessions_opened += 1
        return self.init_response

    async def aclose(self):
        self.closed = True

    async def send(self, message, timeout_s=None):
        self.timings[message.get("method")] = hamcp_mod.Timing(200, 1, 2, 3)
        self.sent.append(message)
        if self.error:
            raise self.error
        if "id" not in message:
            return None
        if message["method"] in self.rpc_errors:
            return {"jsonrpc": "2.0", "id": message["id"], "error": self.rpc_errors[message["method"]]}
        return {"jsonrpc": "2.0", "id": message["id"], "result": self.responses.get(message["method"], {})}


TOOLS = {"tools": [{"name": "ha_get_state"}, {"name": "ha_config_set_automation"}, {"name": "ha_call_service"}]}
INIT_RESULT = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "ha-mcp"}}


def ready_catalog(ha, tools=TOOLS):
    catalog = ToolCatalog(ha)
    catalog.init_result = INIT_RESULT
    catalog.tools = list(tools["tools"])
    catalog.fetched_at = time.time()
    catalog.ready.set()
    return catalog


def make_bridge(ha, catalog=None):
    out = []

    async def send(raw):
        out.append(json.loads(raw))

    return bridge_mod.Bridge(VOICE, ha, catalog if catalog is not None else ToolCatalog(ha), send), out


async def test_tools_list_is_filtered_from_the_catalog():
    ha = FakeHa()
    bridge, out = make_bridge(ha, ready_catalog(ha))
    await bridge.handle(json.dumps({"jsonrpc": "2.0", "id": "u_2", "method": "tools/list"}))
    assert out == [
        {"jsonrpc": "2.0", "id": "u_2", "result": {"tools": [{"name": "ha_get_state"}, {"name": "ha_call_service"}]}}
    ]
    assert ha.sent == []


async def test_tools_list_without_catalog_fetches_once():
    ha = FakeHa({"initialize": INIT_RESULT, "tools/list": TOOLS})
    bridge, out = make_bridge(ha)
    for request_id in ("u_1", "u_2"):
        await bridge.handle(json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "tools/list"}))
    assert [m["method"] for m in ha.sent] == ["initialize", "tools/list"]
    assert [[t["name"] for t in o["result"]["tools"]] for o in out] == [["ha_get_state", "ha_call_service"]] * 2


async def test_endpoint_initialize_is_answered_without_a_new_session():
    ha = FakeHa()
    bridge, out = make_bridge(ha, ready_catalog(ha))
    await bridge.handle(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    await bridge.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    assert out == [{"jsonrpc": "2.0", "id": 1, "result": INIT_RESULT}]
    assert ha.sent == []


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


async def test_allowed_calls_are_forwarded_unchanged_over_one_session():
    ha = FakeHa({"tools/call": {"content": [{"type": "text", "text": "done"}]}})
    bridge, out = make_bridge(ha)
    message = {
        "jsonrpc": "2.0",
        "id": "u_6",
        "method": "tools/call",
        "params": {"name": "ha_call_service", "arguments": {"domain": "light", "service": "turn_on"}},
    }
    await bridge.handle(json.dumps(message))
    await bridge.handle(json.dumps(message))
    assert [m["method"] for m in ha.sent] == ["initialize", "tools/call", "tools/call"]
    assert ha.sent[1] == message
    assert out == [{"jsonrpc": "2.0", "id": "u_6", "result": {"content": [{"type": "text", "text": "done"}]}}] * 2


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
        self._bridge = bridge_mod.Bridge(VOICE, ha, ToolCatalog(ha), self._reply)

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
    await bridge.handle(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 3}}))
    assert ha.sent[-1]["method"] == "notifications/cancelled"
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


async def test_announce_registers_filtered_tools_from_the_catalog():
    ha = FakeHa()
    bridge, out = make_bridge(ha, ready_catalog(ha))
    assert await bridge.announce() == 2
    assert ha.sent == []
    assert out[0] == {"jsonrpc": "2.0", "id": bridge_mod.ANNOUNCE_INIT_ID, "result": INIT_RESULT}
    assert out[1]["id"] == bridge_mod.ANNOUNCE_TOOLS_ID
    assert [t["name"] for t in out[1]["result"]["tools"]] == ["ha_get_state", "ha_call_service"]
    assert bridge.announced


async def test_announce_waits_for_the_first_refresh():
    ha = FakeHa({"initialize": INIT_RESULT, "tools/list": TOOLS})
    catalog = ToolCatalog(ha)
    bridge, out = make_bridge(ha, catalog)
    announce = asyncio.create_task(bridge.announce())
    await asyncio.sleep(0)
    assert not announce.done() and out == []
    await catalog.refresh()
    assert await announce == 2
    assert out[0]["result"] == INIT_RESULT


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
    """Runs bridge.run against a scripted connect: each entry is an exception to raise, a socket to
    connect with, or None to connect with a socket that closes at once."""
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
        if isinstance(step, BaseException):
            raise step
        return step if step is not None else FakeWs()

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


# --- heartbeat ----------------------------------------------------------------


class ScriptedHa:
    """ha.send for pings: each step is a response dict, None, or an exception to raise."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.timings = {}

    async def ensure_session(self):
        return {"result": {}}

    async def send(self, message):
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(message) if callable(step) else step


def _ok(message):
    return {"jsonrpc": "2.0", "id": message["id"], "result": {}}


async def _beats(monkeypatch, tmp_path, steps):
    """Runs _heartbeat for len(steps) pings; returns the health file state after each."""
    path = tmp_path / "healthy"
    path.write_text("0")
    seen = []
    count = {"n": 0}

    async def sleep(_seconds):
        if count["n"]:
            seen.append(path.exists())
        if count["n"] == len(steps):
            raise StopLoop
        count["n"] += 1
        if path.exists():
            old = time.time() - 600
            os.utime(path, (old, old))

    monkeypatch.setattr(bridge_mod.asyncio, "sleep", sleep)
    with pytest.raises(StopLoop):
        await bridge_mod._heartbeat(ScriptedHa(steps), str(path))
    return seen, path


@pytest.mark.parametrize(
    "step",
    [
        lambda m: {"jsonrpc": "2.0", "id": m["id"], "error": {"code": -32603, "message": "boom"}},
        None,
        lambda m: {"jsonrpc": "2.0", "id": m["id"]},
        lambda m: {"jsonrpc": "2.0", "id": "other", "result": {}},
        HaMcpError("ConnectError"),
        HaMcpError("HTTP 404"),
    ],
    ids=["rpc-error", "no-response", "missing-result", "other-id", "disconnect", "stale-session"],
)
async def test_heartbeat_failure_clears_health(monkeypatch, tmp_path, step):
    seen, _ = await _beats(monkeypatch, tmp_path, [step])
    assert seen == [False]


async def test_heartbeat_recovers_after_failures(monkeypatch, tmp_path):
    steps = [None, HaMcpError("ConnectError"), _ok, _ok]
    seen, path = await _beats(monkeypatch, tmp_path, steps)
    assert seen == [False, False, True, True]
    assert healthy(str(path), time.time())


def test_ping_ok_needs_a_matching_result():
    assert bridge_mod.ping_ok({"jsonrpc": "2.0", "id": bridge_mod.PING_ID, "result": {}})
    assert not bridge_mod.ping_ok({"jsonrpc": "2.0", "id": bridge_mod.PING_ID, "result": {}, "error": {}})
    assert not bridge_mod.ping_ok({"jsonrpc": "2.0", "id": bridge_mod.PING_ID, "result": None})
    assert not bridge_mod.ping_ok(None)


async def test_stale_session_ping_clears_and_next_ping_recovers():
    calls = []

    def handler(request):
        calls.append(request.headers.get("mcp-session-id"))
        if len(calls) == 1:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": "x", "result": {}}, headers={"mcp-session-id": "s1"})
        if len(calls) == 2:
            return httpx.Response(404, json={})
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(handler))
    await client.send({"jsonrpc": "2.0", "id": "x", "method": "initialize"})
    ping = {"jsonrpc": "2.0", "id": bridge_mod.PING_ID, "method": "ping"}
    with pytest.raises(HaMcpError):
        await client.send(ping)
    assert bridge_mod.ping_ok(await client.send(ping))
    assert calls == [None, "s1", None]
    await client.aclose()


# --- tunnel interface ------------------------------------------------------------


async def test_bridge_waits_for_the_tunnel_interface_before_connecting(reconnect):
    reconnect["config"] = bridge_mod.BridgeConfig(HA_URL, ENDPOINT, VOICE, reconnect["config"].health_file, "wg0")
    reconnect["interfaces"] = [{"lo", "eth0"}, {"lo", "eth0"}, {"lo", "eth0", "wg0"}, {"lo", "eth0"}]
    reconnect["script"] = [OSError("unreachable")]
    with pytest.raises(bridge_mod.BridgeExit) as e:
        await bridge_mod.run(reconnect["config"], reconnect["ha"])
    assert str(e.value) == "wg0 is gone from the network namespace"
    assert reconnect["connects"] == 1
    assert reconnect["sleeps"][:2] == [bridge_mod.IFACE_POLL_S, bridge_mod.IFACE_POLL_S]


def test_load_config_reads_the_tunnel_interface(tmp_path):
    env = {"HA_MCP_URL": HA_URL, "MCP_ENDPOINT": ENDPOINT, "VOICE_FILTER_FILE": str(FILTER_FILE), "TUNNEL_IFACE": "wg0"}
    assert load_config(env).tunnel_iface == "wg0"
    assert load_config({**env, "TUNNEL_IFACE": ""}).tunnel_iface == ""


# --- ha-mcp session hygiene -------------------------------------------------------


class FakeHaMcp:
    """MockTransport handler for a Streamable HTTP MCP server that records sessions, DELETEs and methods.
    Stateful: issues Mcp-Session-Id on initialize and answers 404 for unknown ids. Stateless (as ha-mcp's
    embedded server): no session id, DELETE answers 405."""

    def __init__(self, stateful=True, tools=TOOLS):
        self.stateful = stateful
        self.tools = tools
        self.open = set()
        self.max_open = 0
        self.initializes = 0
        self.deletes = []
        self.methods = []
        self._ids = itertools.count(1)

    def _sse(self, message, headers=None):
        body = f"event: message\r\ndata: {json.dumps(message)}\r\n\r\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream", **(headers or {})}, text=body)

    def __call__(self, request):
        sid = request.headers.get("mcp-session-id")
        if request.method == "DELETE":
            self.deletes.append(sid)
            if not self.stateful:
                return httpx.Response(405, json={"jsonrpc": "2.0", "id": None, "error": {"code": -32600}})
            if sid not in self.open:
                return httpx.Response(404)
            self.open.discard(sid)
            return httpx.Response(200)
        body = json.loads(request.content)
        method = body.get("method")
        self.methods.append(method)
        if method == "initialize":
            self.initializes += 1
            headers = {}
            if self.stateful:
                sid = f"{next(self._ids):08d}" + "f" * 24
                self.open.add(sid)
                self.max_open = max(self.max_open, len(self.open))
                headers["mcp-session-id"] = sid
            result = {"protocolVersion": "2024-11-05", "serverInfo": {"name": "fake-ha-mcp"}}
            return self._sse({"jsonrpc": "2.0", "id": body["id"], "result": result}, headers)
        if self.stateful and sid not in self.open:
            return httpx.Response(404 if sid else 400)
        if "id" not in body:
            return httpx.Response(202)
        if method == "tools/list":
            return self._sse({"jsonrpc": "2.0", "id": body["id"], "result": self.tools})
        if method == "tools/call":
            result = {"content": [{"type": "text", "text": "on"}], "isError": False}
            return self._sse({"jsonrpc": "2.0", "id": body["id"], "result": result})
        return self._sse({"jsonrpc": "2.0", "id": body["id"], "result": {}})


def fake_client(server):
    return HaMcpClient(HA_URL, transport=httpx.MockTransport(server))


ENDPOINT_SESSION = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
    {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ha_get_state", "arguments": {}}},
]


class ScriptedWs(FakeWs):
    """Endpoint socket that delivers `messages`, then closes once `replies` messages came back."""

    def __init__(self, messages, replies):
        super().__init__()
        self._messages = [json.dumps(m) for m in messages]
        self._replies = replies
        self._done = asyncio.Event()

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        if len(self.sent) >= self._replies:
            self._done.set()

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.wait_for(self._done.wait(), 5)
        raise StopAsyncIteration


async def test_client_reuses_one_session_and_deletes_it_on_close():
    server = FakeHaMcp()
    client = fake_client(server)
    first = await client.ensure_session()
    assert await client.ensure_session() is first
    for _ in range(3):
        assert bridge_mod.ping_ok(await client.send({"jsonrpc": "2.0", "id": bridge_mod.PING_ID, "method": "ping"}))
    sid = client.session_id
    assert server.initializes == 1 and server.open == {sid}
    await client.aclose()
    assert server.deletes == [sid] and server.open == set()


async def test_stateless_server_gets_one_initialize_and_no_delete():
    server = FakeHaMcp(stateful=False)
    client = fake_client(server)
    await client.ensure_session()
    await client.ensure_session()
    await client.send({"jsonrpc": "2.0", "id": "p", "method": "ping"})
    assert client.session_id is None
    assert hamcp_mod.session_label(client.session_id) == "stateless"
    await client.aclose()
    assert server.initializes == 1 and server.deletes == []


async def test_expired_session_reopens_once_without_delete():
    server = FakeHaMcp()
    client = fake_client(server)
    await client.ensure_session()
    server.open.clear()
    with pytest.raises(HaMcpError, match="session expired"):
        await client.send({"jsonrpc": "2.0", "id": "p", "method": "ping"})
    await client.ensure_session()
    assert bridge_mod.ping_ok(await client.send({"jsonrpc": "2.0", "id": bridge_mod.PING_ID, "method": "ping"}))
    assert server.initializes == 2 and server.deletes == [] and client.sessions_opened == 2
    await client.aclose()


async def test_failed_handshake_deletes_the_half_open_session():
    server = FakeHaMcp()

    def handler(request):
        if request.content and json.loads(request.content).get("method") == "notifications/initialized":
            return httpx.Response(500)
        return server(request)

    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(handler))
    with pytest.raises(HaMcpError):
        await client.ensure_session()
    half_open = next(iter(server.open))
    with pytest.raises(HaMcpError):
        await client.ensure_session()
    assert half_open in server.deletes
    await client.aclose()
    assert server.open == set()


async def test_tools_list_is_bounded_even_when_the_body_stalls(monkeypatch):
    monkeypatch.setitem(hamcp_mod.METHOD_TIMEOUT_S, "tools/list", 0.2)

    async def stalled_body():
        yield b"event: message\r\ndata: {"
        await asyncio.Event().wait()

    async def handler(request):
        body = json.loads(request.content)
        if body["method"] == "tools/list":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stalled_body())
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}})

    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(handler))
    started = time.monotonic()
    with pytest.raises(HaMcpError) as e:
        await client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert str(e.value) == "tools/list timed out after 0.2 s"
    assert time.monotonic() - started < 2
    assert bridge_mod.ping_ok(await client.send({"jsonrpc": "2.0", "id": bridge_mod.PING_ID, "method": "ping"}))
    await client._client.aclose()


async def test_timings_record_first_byte_total_and_size():
    client = fake_client(FakeHaMcp())
    await client.ensure_session()
    await client.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    timing = client.timings["tools/list"]
    assert timing.status == 200 and timing.size > 100
    assert 0 <= timing.first_byte_ms <= timing.total_ms
    await client.aclose()


# --- tool catalog -------------------------------------------------------------------


async def test_catalog_refresh_writes_cache_and_load_reads_it(tmp_path):
    path = tmp_path / "cache" / "tools.json"
    client = fake_client(FakeHaMcp())
    catalog = ToolCatalog(client, str(path))
    assert not catalog.load()
    assert await catalog.refresh() is True
    assert await catalog.refresh() is False
    await client.aclose()
    again = ToolCatalog(FakeHa(), str(path))
    assert again.load() and again.ready.is_set()
    assert again.tools == TOOLS["tools"]
    assert again.init_result["serverInfo"]["name"] == "fake-ha-mcp"


@pytest.mark.parametrize("content", ["{", "[]", json.dumps({"initialize": {}, "tools": "x", "fetched_at": 1})])
def test_catalog_ignores_a_bad_cache(tmp_path, content):
    path = tmp_path / "tools.json"
    path.write_text(content)
    catalog = ToolCatalog(FakeHa(), str(path))
    assert not catalog.load() and not catalog.ready.is_set()


async def test_catalog_follows_next_cursor():
    pages = {None: {"tools": [{"name": "a"}], "nextCursor": "p2"}, "p2": {"tools": [{"name": "b"}]}}

    class PagedHa(FakeHa):
        async def send(self, message, timeout_s=None):
            if message["method"] == "tools/list":
                self.sent.append(message)
                self.timings["tools/list"] = hamcp_mod.Timing(200, 1, 2, 3)
                cursor = (message.get("params") or {}).get("cursor")
                return {"jsonrpc": "2.0", "id": message["id"], "result": pages[cursor]}
            return await super().send(message, timeout_s)

    catalog = ToolCatalog(PagedHa({"initialize": INIT_RESULT}))
    await catalog.refresh()
    assert [t["name"] for t in catalog.tools] == ["a", "b"]


async def test_refresh_loop_backs_off_without_storming(monkeypatch):
    delays = []

    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 9:
            raise StopLoop

    monkeypatch.setattr(catalog_mod, "_sleep", sleep)
    ha = FakeHa(error=HaMcpError("tools/list timed out after 20 s"))
    with pytest.raises(StopLoop):
        await catalog_mod.refresh_loop(ToolCatalog(ha), None)
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert len(ha.sent) == 9


async def test_refresh_loop_reannounces_only_on_change(monkeypatch):
    delays, changes = [], []
    lists = [TOOLS, TOOLS, {"tools": [{"name": "ha_get_state"}]}]

    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 3:
            raise StopLoop

    class ChangingHa(FakeHa):
        async def send(self, message, timeout_s=None):
            if message["method"] == "tools/list":
                self.responses["tools/list"] = lists.pop(0)
            return await super().send(message, timeout_s)

    async def on_change():
        changes.append(len(delays))

    monkeypatch.setattr(catalog_mod, "_sleep", sleep)
    with pytest.raises(StopLoop):
        await catalog_mod.refresh_loop(ToolCatalog(ChangingHa({"initialize": INIT_RESULT})), on_change)
    assert delays == [catalog_mod.TOOLS_REFRESH_S] * 3
    assert changes == [0, 2]


# --- restart cycles ------------------------------------------------------------------


@pytest.fixture
def idle_heartbeat(monkeypatch):
    """The reconnect fixture's sleep returns at once; the heartbeat would ping in a tight loop."""

    async def heartbeat(*args):
        await asyncio.Event().wait()

    monkeypatch.setattr(bridge_mod, "_heartbeat", heartbeat)


async def test_fifty_endpoint_reconnects_share_one_ha_mcp_session(reconnect, idle_heartbeat, tmp_path):
    server = FakeHaMcp()
    client = fake_client(server)
    sockets = [ScriptedWs(ENDPOINT_SESSION, replies=5) for _ in range(50)]
    reconnect["script"] = list(sockets)
    with pytest.raises(bridge_mod.BridgeExit):
        await bridge_mod.run(reconnect["config"], client)
    assert server.initializes == 1 and server.max_open == 1
    assert server.methods.count("tools/list") == 1
    assert server.methods.count("tools/call") == 50
    assert server.open == set() and len(server.deletes) == 1
    for ws in sockets:
        assert {m["id"] for m in ws.sent} == {bridge_mod.ANNOUNCE_INIT_ID, bridge_mod.ANNOUNCE_TOOLS_ID, 1, 2, 3}


async def test_fifty_bridge_restarts_keep_one_session_each_and_delete_it(reconnect, idle_heartbeat, tmp_path):
    server = FakeHaMcp()
    cache = tmp_path / "cache" / "ha-mcp-tools.json"
    config = bridge_mod.BridgeConfig(HA_URL, ENDPOINT, VOICE, str(tmp_path / "healthy"), tools_cache=str(cache))
    for cycle in range(50):
        ws = ScriptedWs(ENDPOINT_SESSION[3:], replies=3)
        reconnect["script"] = [ws]
        initializes, deletes = server.initializes, len(server.deletes)
        with pytest.raises(bridge_mod.BridgeExit):
            await bridge_mod.run(config, fake_client(server))
        assert server.initializes - initializes == 1, cycle
        assert len(server.deletes) - deletes == 1, cycle
        assert server.open == set(), cycle
        ids = [m["id"] for m in ws.sent]
        assert bridge_mod.ANNOUNCE_TOOLS_ID in ids and 3 in ids, cycle
        if cycle:
            assert ids[:2] == [bridge_mod.ANNOUNCE_INIT_ID, bridge_mod.ANNOUNCE_TOOLS_ID], cycle
        assert cache.exists()
    assert server.max_open == 1
    assert len(set(server.deletes)) == 50


async def test_cancelled_run_deletes_the_session(tmp_path):
    server = FakeHaMcp()
    client = fake_client(server)
    await client.ensure_session()
    config = bridge_mod.BridgeConfig(HA_URL, ENDPOINT, VOICE, str(tmp_path / "healthy"), tunnel_iface="wg-never")
    task = asyncio.create_task(bridge_mod.run(config, client))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(server.deletes) == 1 and server.open == set()


async def test_stale_cache_registers_tools_while_tools_list_hangs(reconnect, idle_heartbeat, tmp_path, monkeypatch):
    monkeypatch.setitem(hamcp_mod.METHOD_TIMEOUT_S, "tools/list", 0.2)
    cache = tmp_path / "tools.json"
    cache.write_text(json.dumps({"fetched_at": 1, "initialize": INIT_RESULT, "tools": TOOLS["tools"]}))
    server = FakeHaMcp(stateful=False)

    async def handler(request):
        if request.content and json.loads(request.content).get("method") == "tools/list":
            await asyncio.Event().wait()
        return server(request)

    client = HaMcpClient(HA_URL, transport=httpx.MockTransport(handler))
    ws = ScriptedWs(ENDPOINT_SESSION, replies=5)
    reconnect["script"] = [ws]
    config = bridge_mod.BridgeConfig(HA_URL, ENDPOINT, VOICE, str(tmp_path / "healthy"), tools_cache=str(cache))
    with pytest.raises(bridge_mod.BridgeExit):
        await bridge_mod.run(config, client)
    by_id = {m["id"]: m for m in ws.sent}
    voice_tools = by_id[bridge_mod.ANNOUNCE_TOOLS_ID]["result"]["tools"]
    assert [t["name"] for t in voice_tools] == ["ha_get_state", "ha_call_service"]
    assert by_id[1]["result"] == INIT_RESULT
    assert by_id[2]["result"]["tools"] == voice_tools
    assert by_id[3]["result"]["content"][0]["text"] == "on"


def test_stats_line_is_key_value():
    ha = FakeHa()
    ha.sessions_opened = 1
    ha.timings["tools/list"] = hamcp_mod.Timing(200, 110, 127, 261685)
    catalog = ready_catalog(ha)
    line = bridge_mod.stats_line(ha, catalog, [100, 120, 300])
    fields = dict(kv.split("=") for kv in line.removeprefix("ha-mcp stats ").split())
    assert fields["session"] == "stateless" and fields["sessions_opened"] == "1"
    assert fields["ping_first_byte_ms_p50"] == "120" and fields["ping_first_byte_ms_max"] == "300"
    assert fields["tools_list_first_byte_ms"] == "110" and fields["tools_list_total_ms"] == "127"
    assert fields["tools"] == "3" and int(fields["tools_age_s"]) <= 1


async def test_heartbeat_marks_health_only_after_announce(monkeypatch, tmp_path):
    path = tmp_path / "healthy"
    ha = FakeHa()
    bridge, _ = make_bridge(ha, ready_catalog(ha))
    beats = {"n": 0}

    async def sleep(_seconds):
        beats["n"] += 1
        if beats["n"] == 2:
            assert not path.exists()
            await bridge.announce()
        if beats["n"] == 3:
            raise StopLoop

    monkeypatch.setattr(bridge_mod.asyncio, "sleep", sleep)
    with pytest.raises(StopLoop):
        await bridge_mod._heartbeat(ha, str(path), bridge)
    assert path.exists()


async def test_bridge_is_stale_only_after_a_later_change():
    ha = FakeHa()
    catalog = ready_catalog(ha)
    bridge, out = make_bridge(ha, catalog)
    assert not bridge.stale
    await bridge.announce()
    assert not bridge.stale
    catalog.tools = [{"name": "ha_get_state"}]
    assert bridge.stale
    assert await bridge.announce_tools() == 1
    assert not bridge.stale and len(out) == 3


@pytest.mark.parametrize("ping_error, healthy_after", [(None, True), (HaMcpError("ping timed out after 10 s"), False)])
async def test_announce_marks_health_only_when_ha_mcp_answers(tmp_path, ping_error, healthy_after):
    path = tmp_path / "healthy"

    class PingHa(FakeHa):
        async def send(self, message, timeout_s=None):
            if message["method"] == "ping" and ping_error:
                raise ping_error
            return await super().send(message, timeout_s)

    ha = PingHa()
    bridge, out = make_bridge(ha, ready_catalog(ha))
    await bridge_mod._announce(bridge, ha, str(path))
    assert path.exists() is healthy_after
    assert [m["id"] for m in out] == [bridge_mod.ANNOUNCE_INIT_ID, bridge_mod.ANNOUNCE_TOOLS_ID]
