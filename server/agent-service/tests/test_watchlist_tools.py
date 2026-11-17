import socket
import threading
import time

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer

from weixue_agent.config import load_settings
from weixue_agent.watchlist_tools import UNREACHABLE_TEXT, WatchlistTools

TOKEN = "w" * 40


class Bearer:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and dict(scope["headers"]).get(b"authorization") != f"Bearer {TOKEN}".encode():
            await send({"type": "http.response.start", "status": 401, "headers": []})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def watchlist_url():
    server = MCPServer("watchlist")

    @server.tool(description="Echo the topic back.")
    def watchlist_echo(topic: str) -> dict:
        return {"topic": topic}

    @server.tool(description="Not a watchlist tool.")
    def other_tool() -> str:
        return "x"

    app = Bearer(server.streamable_http_app(streamable_http_path="/mcp/", stateless_http=True, json_response=True))
    port = free_port()
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp/"
    srv.should_exit = True
    thread.join(5)


async def test_lists_prefixed_tools_and_calls_them(watchlist_url):
    tools = WatchlistTools(watchlist_url, TOKEN)
    await tools.refresh()
    assert [t.name for t in tools.mcp_tools()] == ["watchlist_echo"]
    result = await tools.call_tool("watchlist_echo", {"topic": "cold war"})
    assert not result.is_error and "cold war" in result.content[0].text


async def test_wrong_token_and_unreachable_return_error_results(watchlist_url):
    bad = WatchlistTools(watchlist_url, "nope")
    with pytest.raises(Exception):
        await bad.refresh()
    result = await bad.call_tool("watchlist_echo", {"topic": "x"})
    assert result.is_error and result.content[0].text == UNREACHABLE_TEXT

    gone = WatchlistTools(f"http://127.0.0.1:{free_port()}/mcp/", TOKEN)
    result = await gone.call_tool("watchlist_echo", {"topic": "x"})
    assert result.is_error and result.content[0].text == UNREACHABLE_TEXT


def test_disabled_without_url_or_token():
    assert not WatchlistTools(None, TOKEN).enabled
    assert not WatchlistTools("http://watchlist:8090/mcp/", None).enabled


def test_settings_read_watchlist_env():
    env = {"AGENT_SERVICE_TOKEN": "t" * 40, "WATCHLIST_MCP_URL": " http://watchlist:8090/mcp/ ",
           "WATCHLIST_MCP_TOKEN": TOKEN}
    s = load_settings(env)
    assert (s.watchlist_mcp_url, s.watchlist_mcp_token) == ("http://watchlist:8090/mcp/", TOKEN)
    assert load_settings({"AGENT_SERVICE_TOKEN": "t" * 40}).watchlist_mcp_url is None
