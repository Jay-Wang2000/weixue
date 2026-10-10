"""weixue_device_tools: the device MCP client keeps and calls only agent-visible device tools
(ADR-13), against the vendored device_mcp modules."""
import asyncio
import importlib
import json
import re
import sys
import types
from pathlib import Path

import pytest

import weixue_device_tools

POLICY_H = Path(__file__).resolve().parents[2] / "firmware" / "overrides" / "main" / "weixue_mcp_policy.h"

# Names a device build has registered or could register; only the first six are agent-visible.
DEVICE_TOOLS = [
    "self.get_device_status",
    "self.audio_speaker.set_volume",
    "self.screen.set_brightness",
    "self.screen.set_theme",
    "self.screen.set_bedside",
    "self.imu.get_state",
    "self.system.reconfigure_wifi",
    "self.diag.gesture",
    "self.diag.touch",
    "self.diag.simulate_face",
    "self.camera.take_photo",
    "self.reboot",
    "self.upgrade_firmware",
    "self.assets.set_download_url",
]


@pytest.fixture
def device_mcp(monkeypatch):
    """The vendored device_mcp package (core.utils.auth stubbed: tests have no PyJWT), with every
    hook weixue_device_tools.apply() sets restored after the test."""
    auth = types.ModuleType("core.utils.auth")
    auth.AuthToken = object
    monkeypatch.setitem(sys.modules, "core.utils.auth", auth)
    package = importlib.import_module("core.providers.tools.device_mcp")
    client = importlib.import_module("core.providers.tools.device_mcp.mcp_client")
    handler = importlib.import_module("core.providers.tools.device_mcp.mcp_handler")
    executor = importlib.import_module("core.providers.tools.device_mcp.mcp_executor")
    for cls in {client.MCPClient, handler.MCPClient}:
        monkeypatch.setattr(cls, "add_tool", cls.add_tool)
    for module in (handler, executor, package):
        monkeypatch.setattr(module, "call_mcp_tool", module.call_mcp_tool)
    return types.SimpleNamespace(package=package, client=client, handler=handler, executor=executor)


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


def _conn():
    return types.SimpleNamespace(features={"mcp": True}, websocket=Socket())


def _tools_list_reply(names):
    tools = [{"name": n, "description": n, "inputSchema": {"type": "object", "properties": {}}} for n in names]
    return {"jsonrpc": "2.0", "id": 2, "result": {"tools": tools}}


def test_server_list_equals_firmware_policy():
    block = re.search(r"kAgentVisibleMcpTools\[\] = \{(.*?)\};", POLICY_H.read_text(), re.S).group(1)
    assert set(re.findall(r'"([^"]+)"', block)) == weixue_device_tools.AGENT_VISIBLE_DEVICE_TOOLS


@pytest.mark.parametrize("name", DEVICE_TOOLS)
def test_is_agent_visible(name):
    assert weixue_device_tools.is_agent_visible(name) == (DEVICE_TOOLS.index(name) < 6)


@pytest.mark.parametrize("name", [None, "", "self_screen_set_bedside", "SELF.IMU.GET_STATE", 3])
def test_other_names_are_not_agent_visible(name):
    assert not weixue_device_tools.is_agent_visible(name)


async def test_tools_list_keeps_agent_visible_tools_only(device_mcp, log_records):
    weixue_device_tools.apply()
    mcp = device_mcp.package.MCPClient()
    conn = _conn()

    await device_mcp.handler.handle_mcp_message(conn, mcp, _tools_list_reply(DEVICE_TOOLS))

    assert sorted(mcp.name_mapping.values()) == sorted(DEVICE_TOOLS[:6])
    functions = {f["function"]["name"] for f in mcp.get_available_tools()}
    assert functions == {n.replace(".", "_") for n in DEVICE_TOOLS[:6]}
    assert await mcp.is_ready()
    dropped = [m for level, m in log_records if level == "warning" and "dropped" in m]
    assert dropped == [f"device tool {n!r} dropped: not agent-visible" for n in DEVICE_TOOLS[6:]]


async def test_hidden_call_is_refused_before_reaching_the_device(device_mcp, log_records):
    weixue_device_tools.apply()
    mcp = device_mcp.package.MCPClient()
    await mcp.add_tool({"name": "self.screen.set_bedside", "description": "", "inputSchema": {}})
    await mcp.set_ready(True)
    # A hidden tool that reached the client anyway, as if registered past the add_tool hook.
    mcp.tools["self_diag_touch"] = {"name": "self.diag.touch", "description": "", "inputSchema": {}}
    mcp.name_mapping["self_diag_touch"] = "self.diag.touch"
    conn = _conn()

    for module in (device_mcp.handler, device_mcp.executor, device_mcp.package):
        for name in ("self_diag_touch", "self.diag.gesture", "self_system_reconfigure_wifi"):
            with pytest.raises(ValueError, match="is not available"):
                await module.call_mcp_tool(conn, mcp, name, "{}")
    assert conn.websocket.sent == []
    refused = [m for level, m in log_records if level == "warning" and "refused" in m]
    assert refused == [
        "device tool call 'self.diag.touch' refused: not agent-visible",
        "device tool call 'self.diag.gesture' refused: not agent-visible",
        "device tool call 'self_system_reconfigure_wifi' refused: not agent-visible",
    ] * 3

    conn.mcp_client = mcp
    response = await device_mcp.executor.DeviceMCPExecutor(conn).execute(conn, "self_diag_touch", {})
    assert response.action.name == "NOTFOUND"
    assert conn.websocket.sent == []


async def test_agent_visible_call_reaches_the_device(device_mcp):
    weixue_device_tools.apply()
    mcp = device_mcp.package.MCPClient()
    await mcp.add_tool({"name": "self.screen.set_bedside", "description": "", "inputSchema": {}})
    await mcp.set_ready(True)
    conn = _conn()

    async def answer():
        while not conn.websocket.sent:
            await asyncio.sleep(0)
        request = conn.websocket.sent[0]["payload"]
        result = {"content": [{"type": "text", "text": '{"night":true}'}], "isError": False}
        await mcp.resolve_call_result(request["id"], result)

    task = asyncio.create_task(answer())
    text = await device_mcp.executor.call_mcp_tool(conn, mcp, "self_screen_set_bedside", '{"mode":"night"}')
    await task
    assert text == '{"night":true}'
    assert conn.websocket.sent[0]["payload"]["params"] == {
        "name": "self.screen.set_bedside",
        "arguments": {"mode": "night"},
    }


def test_apply_wraps_each_hook_once(device_mcp):
    upstream_add = device_mcp.client.MCPClient.add_tool
    upstream_calls = {m: m.call_mcp_tool for m in (device_mcp.handler, device_mcp.executor, device_mcp.package)}

    weixue_device_tools.apply()
    weixue_device_tools.apply()

    assert device_mcp.client.MCPClient.add_tool.weixue_wrapped is upstream_add
    assert device_mcp.package.MCPClient is device_mcp.client.MCPClient
    for module, upstream in upstream_calls.items():
        assert module.call_mcp_tool.weixue_wrapped is upstream
