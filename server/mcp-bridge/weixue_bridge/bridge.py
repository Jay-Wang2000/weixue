"""Tool-side client of the MCP endpoint that forwards each JSON-RPC message to ha-mcp."""
import asyncio
import json
import logging
import os
import signal
import socket
import statistics
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set

import websockets

from .config import (
    ENDPOINT_MAX_FAILURES,
    HEARTBEAT_S,
    IFACE_POLL_S,
    NAMESPACE_IFACE,
    BridgeConfig,
    VoiceFilter,
    redact,
)
from .catalog import ToolCatalog, refresh_loop
from .hamcp import HaMcpClient, HaMcpError, session_label
from .status import BridgeStatus
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
# Pings per `ha-mcp stats` log line.
STATS_EVERY_PINGS = 10


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
    """One endpoint connection. initialize and tools/list are answered from the catalog, so endpoint
    clients share the bridge's single ha-mcp session; everything else is forwarded over that session."""

    def __init__(
        self,
        voice_filter: VoiceFilter,
        ha: HaMcpClient,
        catalog: ToolCatalog,
        send: Callable[[str], Awaitable[None]],
    ):
        self._filter = voice_filter
        self._ha = ha
        self._catalog = catalog
        self._send_raw = send
        self._send_lock = asyncio.Lock()
        self.announced = False
        self._announced_tools: Optional[List[Dict[str, Any]]] = None

    async def _send(self, message: Dict[str, Any]) -> None:
        async with self._send_lock:
            await self._send_raw(json.dumps(message, ensure_ascii=False))

    def _tools_response(self, request_id: Any) -> Dict[str, Any]:
        response = {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self._catalog.tools or []}}
        return with_filtered_tools(response, self._filter)

    @property
    def stale(self) -> bool:
        """Announced, and the catalog has changed since."""
        return self.announced and self._announced_tools != self._catalog.tools

    async def announce(self) -> int:
        """Once the catalog holds ha-mcp's tools, registers ha-mcp and its filtered tool list with the endpoint."""
        await self._catalog.ready.wait()
        await self._send({"jsonrpc": "2.0", "id": ANNOUNCE_INIT_ID, "result": self._catalog.init_result})
        count = await self.announce_tools()
        self.announced = True
        return count

    async def announce_tools(self) -> int:
        self._announced_tools = self._catalog.tools
        tools = self._tools_response(ANNOUNCE_TOOLS_ID)
        await self._send(tools)
        return len(tools["result"]["tools"])

    async def _local(self, method: str, request_id: Any) -> None:
        """initialize and tools/list without opening another ha-mcp session."""
        try:
            if method == "initialize":
                result = self._catalog.init_result or (await self._ha.ensure_session())["result"]
                await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})
                return
            if not self._catalog.ready.is_set():
                await self._catalog.refresh()
        except HaMcpError as e:
            log.warning("%s failed: %s", method, e)
            await self._send(rpc_error(request_id, f"ha-mcp unavailable ({e})"))
            return
        await self._send(self._tools_response(request_id))

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
        if method == "notifications/initialized":
            return
        if method in ("initialize", "tools/list"):
            if request_id is not None:
                await self._local(method, request_id)
            return
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
            await self._ha.ensure_session()
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


def ping_ok(response: Optional[Dict[str, Any]]) -> bool:
    """A JSON-RPC success result for PING_ID."""
    return (
        isinstance(response, dict)
        and response.get("id") == PING_ID
        and "error" not in response
        and isinstance(response.get("result"), dict)
    )


def stats_line(ha: HaMcpClient, catalog: ToolCatalog, ping_ms: List[int]) -> str:
    """key=value summary of the ha-mcp session and latencies for the monitor."""
    tools_list = ha.timings.get("tools/list")
    fields = [
        f"session={session_label(ha.session_id)}",
        f"sessions_opened={ha.sessions_opened}",
        f"ping_first_byte_ms_p50={round(statistics.median(ping_ms)) if ping_ms else -1}",
        f"ping_first_byte_ms_max={max(ping_ms) if ping_ms else -1}",
        f"tools_list_first_byte_ms={tools_list.first_byte_ms if tools_list else -1}",
        f"tools_list_total_ms={tools_list.total_ms if tools_list else -1}",
        f"tools={len(catalog.tools) if catalog.tools is not None else -1}",
        f"tools_age_s={round(time.time() - catalog.fetched_at) if catalog.fetched_at else -1}",
    ]
    return "ha-mcp stats " + " ".join(fields)


async def _ping(ha: HaMcpClient) -> Optional[str]:
    """Pings ha-mcp over the held session; None on a matching success result, else the failure."""
    try:
        await ha.ensure_session()
        response = await ha.send({"jsonrpc": "2.0", "id": PING_ID, "method": "ping"})
    except HaMcpError as e:
        return str(e)
    if ping_ok(response):
        return None
    return "error result" if isinstance(response, dict) and "error" in response else "no result"


async def _heartbeat(
    ha: HaMcpClient,
    health_file: str,
    bridge: Optional[Bridge] = None,
    catalog: Optional[ToolCatalog] = None,
    status: Optional[BridgeStatus] = None,
) -> None:
    """Every HEARTBEAT_S pings ha-mcp. Success refreshes the health file once `bridge` has announced,
    failure removes it; every ping rewrites the status file; every STATS_EVERY_PINGS successful pings
    with a catalog log stats_line."""
    ping_ms: List[int] = []
    while True:
        await asyncio.sleep(HEARTBEAT_S)
        failure = await _ping(ha)
        first_byte = ha.timings["ping"].first_byte_ms if failure is None and "ping" in ha.timings else None
        if failure is None:
            if first_byte is not None:
                ping_ms.append(first_byte)
            if bridge is None or bridge.announced:
                _mark_healthy(health_file)
        else:
            log.warning("ha-mcp ping failed: %s", failure)
            _clear_health(health_file)
        if status is not None:
            status.ping(failure is None, first_byte)
        if catalog is not None and len(ping_ms) >= STATS_EVERY_PINGS:
            log.info("%s", stats_line(ha, catalog, ping_ms))
            ping_ms = []


def _interface_names() -> Set[str]:
    return {name for _, name in socket.if_nameindex()}


async def wait_for_interface(name: str) -> None:
    """Returns once the network namespace has interface `name`, polling every IFACE_POLL_S."""
    logged = False
    while name not in _interface_names():
        if not logged:
            log.info("waiting for %s before connecting", name)
            logged = True
        await asyncio.sleep(IFACE_POLL_S)


async def _announce(bridge: Bridge, ha: HaMcpClient, health_file: str, status: Optional[BridgeStatus] = None) -> None:
    """Registers the tools, then marks the bridge healthy once ha-mcp answers a ping."""
    try:
        count = await bridge.announce()
    except websockets.exceptions.WebSocketException:
        return
    log.info("connected; %d ha-mcp tools on the voice path", count)
    failure = await _ping(ha)
    if failure is None:
        _mark_healthy(health_file)
    else:
        log.warning("ha-mcp ping failed: %s", failure)
    if status is not None:
        status.announced = True
        status.ping(failure is None, ha.timings["ping"].first_byte_ms if failure is None and "ping" in ha.timings else None)


async def run(
    config: BridgeConfig,
    ha: Optional[HaMcpClient] = None,
    catalog: Optional[ToolCatalog] = None,
    status: Optional[BridgeStatus] = None,
) -> None:
    """Keeps the endpoint connection up; raises BridgeExit after ENDPOINT_MAX_FAILURES consecutive failed
    connects, once NAMESPACE_IFACE, present at start, is gone, or once config.tunnel_iface is gone.
    With config.tunnel_iface set nothing is connected before that interface exists.
    One ha-mcp session serves every endpoint connection; it is ended when run returns or is cancelled.
    The status file says `stopped` once run ends."""
    ha = ha or HaMcpClient(config.ha_mcp_url)
    catalog = catalog or ToolCatalog(ha, config.tools_cache)
    status = status or BridgeStatus(config.status_file, ha, catalog)
    log.info("ha-mcp %s <-> endpoint %s", redact(config.ha_mcp_url), redact(config.endpoint_url))
    catalog.load()
    current: Dict[str, Optional[Bridge]] = {"bridge": None}

    async def reannounce() -> None:
        bridge = current["bridge"]
        if bridge is not None and bridge.stale:
            log.info("ha-mcp tool list changed; %d tools on the voice path", await bridge.announce_tools())

    refresher: Optional[asyncio.Task] = None
    status.write()
    try:
        if config.tunnel_iface:
            await wait_for_interface(config.tunnel_iface)
        refresher = asyncio.create_task(refresh_loop(catalog, reannounce))
        watch_iface = NAMESPACE_IFACE in _interface_names()
        backoff = 1
        failures = 0
        while True:
            connected = False
            try:
                async with websockets.connect(config.endpoint_url, ping_interval=20, ping_timeout=20) as ws:
                    connected = True
                    bridge = Bridge(config.voice_filter, ha, catalog, ws.send)
                    current["bridge"] = bridge
                    backoff = 1
                    status.update(state="connected", announced=False)
                    announcer = asyncio.create_task(_announce(bridge, ha, config.health_file, status))
                    heartbeat = asyncio.create_task(_heartbeat(ha, config.health_file, bridge, catalog, status))
                    pending = set()
                    try:
                        async for raw in ws:
                            task = asyncio.create_task(bridge.handle(raw))
                            pending.add(task)
                            task.add_done_callback(pending.discard)
                    finally:
                        current["bridge"] = None
                        tasks = [announcer, heartbeat, *pending]
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                log.warning("endpoint closed the connection")
            except (OSError, websockets.exceptions.WebSocketException) as e:
                log.warning("bridge down: %s", type(e).__name__)
            _clear_health(config.health_file)
            status.update(state="disconnected", announced=False)
            names = _interface_names()
            if watch_iface and NAMESPACE_IFACE not in names:
                raise BridgeExit(f"{NAMESPACE_IFACE} is gone from the network namespace")
            if config.tunnel_iface and config.tunnel_iface not in names:
                raise BridgeExit(f"{config.tunnel_iface} is gone from the network namespace")
            failures = 0 if connected else failures + 1
            if failures >= ENDPOINT_MAX_FAILURES:
                raise BridgeExit(f"endpoint unreachable for {failures} consecutive attempts")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)
    finally:
        status.update(state="stopped", announced=False)
        if refresher is not None:
            refresher.cancel()
            await asyncio.gather(refresher, return_exceptions=True)
        await catalog.aclose()
        await ha.aclose()


async def serve(config: BridgeConfig) -> None:
    """run() until SIGTERM or SIGINT."""
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, task.cancel)
    try:
        await run(config)
    except asyncio.CancelledError:
        log.info("stopped")
