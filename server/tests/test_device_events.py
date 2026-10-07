"""weixue_patches.log_device_events: device MCP notifications under notifications/weixue/ are
logged with their params and still reach upstream."""
import sys
import types

import weixue_patches
from tests.test_conversation_timing import stub_handler_modules, stub_listen_and_http


class Logger:
    def __init__(self):
        self.lines = []

    def bind(self, **_kwargs):
        return self

    def info(self, line):
        self.lines.append(line)


async def _upstream(self, conn, msg_json):
    return ("upstream", msg_json)


def _panel(decision):
    return {
        "type": "mcp",
        "payload": {
            "jsonrpc": "2.0",
            "method": "notifications/weixue/panel",
            "params": {"event": "approval_decision", "card_id": "fixture-1", "decision": decision,
                       "fixture": True, "seq": 2},
        },
    }


async def test_panel_decision_is_logged_with_params_and_forwarded():
    logger = Logger()
    handle = weixue_patches.log_device_events(_upstream, logger)
    conn = types.SimpleNamespace(device_id="7c:0c:5f:42:4b:fc")
    msg = _panel("deny")

    assert await handle(None, conn, msg) == ("upstream", msg)
    assert logger.lines == [
        "device event notifications/weixue/panel device=7c:0c:5f:42:4b:fc params="
        '{"card_id": "fixture-1", "decision": "deny", "event": "approval_decision", '
        '"fixture": true, "seq": 2}'
    ]


async def test_other_mcp_messages_are_not_logged():
    logger = Logger()
    handle = weixue_patches.log_device_events(_upstream, logger)
    conn = types.SimpleNamespace(device_id=None)
    messages = [
        {"type": "mcp", "payload": {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}}},
        {"type": "mcp", "payload": {"jsonrpc": "2.0", "method": "notifications/initialized"}},
        {"type": "mcp", "payload": "not-an-object"},
        {"type": "mcp"},
    ]
    for msg in messages:
        assert await handle(None, conn, msg) == ("upstream", msg)
    assert logger.lines == []


async def test_apply_wraps_mcp_handler_once(monkeypatch):
    connection = types.ModuleType("core.connection")

    class ConnectionHandler:
        _clean_response_garbage = staticmethod(lambda text: text)

        async def handle_connection(self, ws):
            return None

    connection.ConnectionHandler = ConnectionHandler
    monkeypatch.setitem(sys.modules, "core.connection", connection)
    stub_handler_modules(monkeypatch)
    stub_listen_and_http(monkeypatch)
    mcp = sys.modules["core.handle.textHandler.mcpMessageHandler"].McpTextMessageHandler
    upstream = mcp.handle

    weixue_patches.apply()
    weixue_patches.apply()

    assert mcp.handle.weixue_wrapped is upstream
    assert await mcp().handle(types.SimpleNamespace(device_id="d"), _panel("approve")) == "upstream"
