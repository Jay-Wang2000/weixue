"""Live smoke tests through the running MCP endpoint and bridge: tools/list, and failure
results driven through the vendored server's endpoint client and executor.

Runs only with RUN_LIVE_TESTS=1 against the compose stack on this host. The endpoint
URL comes from server/data/mcp-bridge/endpoint-url (override with MCP_ENDPOINT_FILE);
the test connects to its caller-side /call/ path on 127.0.0.1:8004.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER_DIR / "mcp-bridge"))

from weixue_bridge.config import load_voice_filter  # noqa: E402

ENDPOINT_FILE = Path(os.environ.get("MCP_ENDPOINT_FILE", SERVER_DIR / "data" / "mcp-bridge" / "endpoint-url"))

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1" or not ENDPOINT_FILE.exists(),
    reason="set RUN_LIVE_TESTS=1 with the stack running (needs data/mcp-bridge/endpoint-url)",
)


def caller_url() -> str:
    url = ENDPOINT_FILE.read_text().strip()
    url = re.sub(r"^ws://[^/]+", "ws://127.0.0.1:8004", url)
    return url.replace("/mcp_endpoint/mcp/", "/mcp_endpoint/call/")


async def rpc(ws, message, timeout=30):
    await ws.send(json.dumps(message))
    while True:
        reply = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if reply.get("id") == message["id"]:
            return reply


async def test_live_tools_list_through_endpoint():
    import websockets

    voice = load_voice_filter(str(SERVER_DIR / "policy" / "voice_bridge.json"))
    async with websockets.connect(caller_url()) as ws:
        init = await rpc(
            ws,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "live-test"}},
            },
        )
        assert "result" in init
        await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}))
        tools = await rpc(ws, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in tools["result"]["tools"]}
        print(f"\nvoice-path tools: {sorted(names)}")
        assert names == set(voice.tools)
        assert all(name.startswith("ha_") for name in names)

        refused = await rpc(
            ws,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "ha_call_service",
                    "arguments": {"domain": "lock", "service": "unlock", "entity_id": "lock.weixue_live_test_absent"},
                },
            },
        )
        assert "disabled on the voice path" in refused["result"]["content"][0]["text"]


@pytest.mark.parametrize(
    "tool, arguments, needles",
    [
        (
            "ha_call_service",
            {"domain": "lock", "service": "unlock", "entity_id": "lock.weixue_live_test_absent"},
            ["disabled on the voice path"],
        ),
        ("ha_get_state", {"entity_id": "light.weixue_live_test_absent"}, ["Tool error: ", "ENTITY_NOT_FOUND"]),
    ],
)
async def test_live_failure_text_reaches_llm_through_server_client(tool, arguments, needles):
    from types import SimpleNamespace

    from core.providers.tools.mcp_endpoint.mcp_endpoint_executor import MCPEndpointExecutor
    from core.providers.tools.mcp_endpoint.mcp_endpoint_handler import connect_mcp_endpoint
    from plugins_func.register import Action

    client = await connect_mcp_endpoint(caller_url())
    assert client is not None
    try:
        for _ in range(100):
            if await client.is_ready():
                break
            await asyncio.sleep(0.1)
        assert client.has_tool(tool)
        conn = SimpleNamespace(mcp_endpoint_client=client)
        response = await MCPEndpointExecutor(conn).execute(conn, tool, arguments)
        print(f"\n{tool}: {response.action.name} {response.result[:160]!r}")
        assert response.action == Action.REQLLM
        assert all(needle in response.result for needle in needles)
    finally:
        await client.close()
