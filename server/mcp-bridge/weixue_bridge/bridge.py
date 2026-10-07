"""Tool-side client of the MCP endpoint that forwards each JSON-RPC message to ha-mcp."""
import asyncio
import json
import logging
import os
import socket
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Set

import websockets

from .config import (
    ENDPOINT_MAX_FAILURES,
    HEARTBEAT_S,
    NAMESPACE_IFACE,
    BridgeConfig,
    VoiceFilter,
    redact,
)
from .hamcp import HaMcpClient, HaMcpError
from .voice_filter import filter_tools, refusal

log = logging.getLogger("weixue_bridge")
# Request URLs carry the secret path; client libraries stay below INFO.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

# Ids of the unsolicited responses that register ha-mcp with the endpoint on connect.
ANNOUNCE_INIT_ID = "weixue-bridge-init"
ANNOUNCE_TOOLS_ID = "weixue-bridge-tools"
PING_ID = "weixue-bridge-ping"
RECONNECT_MAX_S = 30
CLIENT_INFO = {"name": "weixue-mcp-bridge", "version": "1"}
PROTOCOL_VERSION = "2024-11-05"


TOOL_ERROR_PREFIX = "Tool error: "


class BridgeExit(Exception):
    """Unrecoverable in this process; __main__ exits non-zero."""


def tool_text(request_id: Any, text: str) -> Dict[str, Any]:
    """Single-text tools/call result with isError false, so the text reaches the LLM (docs/ha-mcp.md)."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": False},
    }


def _content_text(result: Dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(c["text"] for c in content if isinstance(c, dict) and isinstance(c.get("text"), str))


def for_llm(response: Dict[str, Any]) -> Dict[str, Any]:
    """ha-mcp tools/call response with failures rewritten as tool_text."""
    request_id = response.get("id")
    error = response.get("error")
    if error is not None:
        message = error.get("message") if isinstance(error, dict) else None
        detail = message if isinstance(message, str) and message else "unknown error"
        return tool_text(request_id, TOOL_ERROR_PREFIX + detail)
    result = response.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        return tool_text(request_id, TOOL_ERROR_PREFIX + (_content_text(result) or "no details"))
    return response


def rpc_error(request_id: Any, text: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": text}}


def with_filtered_tools(response: Dict[str, Any], voice_filter: VoiceFilter) -> Dict[str, Any]:
    result = response.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return response
    return {**response, "result": {**result, "tools": filter_tools(result["tools"], voice_filter)}}


class Bridge:
    def __init__(
        self,
        voice_filter: VoiceFilter,
        ha: HaMcpClient,
        send: Callable[[str], Awaitable[None]],
    ):
        self._filter = voice_filter
        self._ha = ha
        self._send_raw = send
        self._send_lock = asyncio.Lock()

    async def _send(self, message: Dict[str, Any]) -> None:
        async with self._send_lock:
            await self._send_raw(json.dumps(message, ensure_ascii=False))

    async def announce(self) -> int:
        """Initialize ha-mcp and register its filtered tool list with the endpoint."""
        init = await self._ha.send(
            {
                "jsonrpc": "2.0",
                "id": ANNOUNCE_INIT_ID,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            }
        )
        await self._ha.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = await self._ha.send({"jsonrpc": "2.0", "id": ANNOUNCE_TOOLS_ID, "method": "tools/list"})
        if not tools or "result" not in tools or not init or "result" not in init:
            raise HaMcpError("initialize or tools/list returned an error")
        tools = with_filtered_tools(tools, self._filter)
        await self._send(init)
        await self._send(tools)
        return len(tools["result"].get("tools", []))

    async def handle(self, raw: str) -> None:
        try:
            message = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("dropped non-JSON message from endpoint")
            return
        if not isinstance(message, dict) or not isinstance(message.get("method"), str):
            return
        method = message["method"]
        request_id = message.get("id")
        tool = None
        if method == "tools/call":
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            tool = params.get("name")
            reason = refusal(self._filter, tool, params.get("arguments") or {})
            if reason:
                log.info("tools/call %s refused: %s", tool, reason)
                await self._send(tool_text(request_id, reason))
                return
        started = time.monotonic()
        try:
            response = await self._ha.send(message)
        except HaMcpError as e:
            log.warning("%s %s failed: %s", method, tool or "", e)
            if request_id is None:
                return
            if method == "tools/call":
                await self._send(tool_text(request_id, "Home Assistant is unreachable right now."))
            else:
                await self._send(rpc_error(request_id, f"ha-mcp unavailable ({e})"))
            return
        if response is None:
            return
        if method == "tools/list":
            response = with_filtered_tools(response, self._filter)
        if method == "tools/call":
            result = response.get("result") if isinstance(response.get("result"), dict) else {}
            outcome = "error" if "error" in response or result.get("isError") else "ok"
            log.info("tools/call %s %s in %d ms", tool, outcome, (time.monotonic() - started) * 1000)
            response = for_llm(response)
        await self._send(response)


def _mark_healthy(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(str(int(time.time())))


def _clear_health(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


async def _heartbeat(ha: HaMcpClient, health_file: str) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        try:
            await ha.send({"jsonrpc": "2.0", "id": PING_ID, "method": "ping"})
            _mark_healthy(health_file)
        except HaMcpError as e:
            log.warning("ha-mcp ping failed: %s", e)
            _clear_health(health_file)


def _interface_names() -> Set[str]:
    return {name for _, name in socket.if_nameindex()}


async def run(config: BridgeConfig, ha: Optional[HaMcpClient] = None) -> None:
    """Keeps the endpoint connection up; raises BridgeExit after ENDPOINT_MAX_FAILURES consecutive failed
    connects, or once NAMESPACE_IFACE, present at start, is gone."""
    ha = ha or HaMcpClient(config.ha_mcp_url)
    log.info("ha-mcp %s <-> endpoint %s", redact(config.ha_mcp_url), redact(config.endpoint_url))
    watch_iface = NAMESPACE_IFACE in _interface_names()
    backoff = 1
    failures = 0
    while True:
        connected = False
        try:
            async with websockets.connect(config.endpoint_url, ping_interval=20, ping_timeout=20) as ws:
                connected = True
                bridge = Bridge(config.voice_filter, ha, ws.send)
                count = await bridge.announce()
                _mark_healthy(config.health_file)
                log.info("connected; %d ha-mcp tools on the voice path", count)
                backoff = 1
                heartbeat = asyncio.create_task(_heartbeat(ha, config.health_file))
                pending = set()
                try:
                    async for raw in ws:
                        task = asyncio.create_task(bridge.handle(raw))
                        pending.add(task)
                        task.add_done_callback(pending.discard)
                finally:
                    heartbeat.cancel()
                    for task in pending:
                        task.cancel()
            log.warning("endpoint closed the connection")
        except (OSError, websockets.exceptions.WebSocketException, HaMcpError) as e:
            log.warning("bridge down: %s", type(e).__name__ if not isinstance(e, HaMcpError) else e)
        _clear_health(config.health_file)
        if watch_iface and NAMESPACE_IFACE not in _interface_names():
            raise BridgeExit(f"{NAMESPACE_IFACE} is gone from the network namespace")
        failures = 0 if connected else failures + 1
        if failures >= ENDPOINT_MAX_FAILURES:
            raise BridgeExit(f"endpoint unreachable for {failures} consecutive attempts")
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX_S)
