"""Streamable HTTP client for ha-mcp: one POST per JSON-RPC message, one MCP session per process."""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

CONNECT_TIMEOUT_S = 5
CALL_TIMEOUT_S = 120
INIT_TIMEOUT_S = 15
TOOLS_LIST_TIMEOUT_S = 20
TERMINATE_TIMEOUT_S = 5
PING_TIMEOUT_S = 10
PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "weixue-mcp-bridge", "version": "1"}
INIT_ID = "weixue-bridge-session"
# Whole-exchange bounds, headers through the last body byte; other methods get CALL_TIMEOUT_S.
METHOD_TIMEOUT_S = {
    "initialize": INIT_TIMEOUT_S,
    "notifications/initialized": INIT_TIMEOUT_S,
    "tools/list": TOOLS_LIST_TIMEOUT_S,
    "ping": PING_TIMEOUT_S,
}

log = logging.getLogger("weixue_bridge")


class HaMcpError(Exception):
    """Transport failure talking to ha-mcp; the message never contains the URL."""


@dataclass(frozen=True)
class Timing:
    """One HTTP exchange; first_byte_ms is the first body chunk, or the headers when the body is empty."""

    status: int
    first_byte_ms: int
    total_ms: int
    size: int


def parse_sse(text: str) -> List[Any]:
    """JSON payloads of the `data:` fields of an event stream, one per event."""
    messages = []
    data_lines: List[str] = []
    for line in text.replace("\r\n", "\n").split("\n") + [""]:
        if line == "":
            if data_lines:
                try:
                    messages.append(json.loads("\n".join(data_lines)))
                except json.JSONDecodeError:
                    pass
                data_lines = []
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip(" "))
    return messages


def session_label(session_id: Optional[str]) -> str:
    """Log form of a Mcp-Session-Id: its first 8 characters, or `stateless` when the server issued none."""
    return f"{session_id[:8]}..." if session_id else "stateless"


class HaMcpClient:
    def __init__(self, url: str, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._url = url
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S), transport=transport
        )
        self._protocol_version: Optional[str] = None
        self._session_id: Optional[str] = None
        self._init_lock = asyncio.Lock()
        self.init_response: Optional[Dict[str, Any]] = None
        self.sessions_opened = 0
        self.timings: Dict[str, Timing] = {}

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    async def aclose(self) -> None:
        await self.terminate()
        await self._client.aclose()

    async def ensure_session(self) -> Dict[str, Any]:
        """The initialize response of the held session; opens one (initialize + initialized) when none is held."""
        async with self._init_lock:
            if self.init_response is not None:
                return self.init_response
            await self.terminate()
            response = await self.send(
                {
                    "jsonrpc": "2.0",
                    "id": INIT_ID,
                    "method": "initialize",
                    "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
                }
            )
            if not response or not isinstance(response.get("result"), dict):
                raise HaMcpError("initialize returned an error")
            await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.init_response = response
            self.sessions_opened += 1
            log.info(
                "ha-mcp session %s opened sessions_opened=%d initialize_first_byte_ms=%d",
                session_label(self._session_id),
                self.sessions_opened,
                self.timings["initialize"].first_byte_ms,
            )
            return response

    async def terminate(self) -> None:
        """Ends the held session with a DELETE carrying its Mcp-Session-Id; a stateless session has nothing to end."""
        session_id, version = self._session_id, self._protocol_version
        self._session_id = None
        self._protocol_version = None
        self.init_response = None
        if not session_id:
            return
        headers = {"Mcp-Session-Id": session_id}
        if version:
            headers["MCP-Protocol-Version"] = version
        try:
            resp = await self._client.delete(
                self._url, headers=headers, timeout=httpx.Timeout(TERMINATE_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
            )
        except httpx.HTTPError as e:
            log.warning("ha-mcp session %s DELETE failed: %s", session_label(session_id), type(e).__name__)
            return
        log.info("ha-mcp session %s DELETE -> HTTP %d", session_label(session_id), resp.status_code)

    async def send(self, message: Dict[str, Any], timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """POST one message; the matching response for a request, None for a notification.
        The whole exchange is bounded by timeout_s, else by METHOD_TIMEOUT_S for its method."""
        method = message.get("method") if isinstance(message.get("method"), str) else ""
        bound = timeout_s if timeout_s is not None else METHOD_TIMEOUT_S.get(method, CALL_TIMEOUT_S)
        try:
            status, headers, body = await asyncio.wait_for(self._post(method, message), bound)
        except asyncio.TimeoutError:
            raise HaMcpError(f"{method or 'request'} timed out after {bound:g} s") from None
        if status == 404 and self._session_id:
            log.warning("ha-mcp session %s expired (HTTP 404)", session_label(self._session_id))
            self._session_id = None
            self._protocol_version = None
            self.init_response = None
            raise HaMcpError("HTTP 404 (session expired)")
        if method == "initialize" and headers.get("mcp-session-id"):
            self._session_id = headers["mcp-session-id"]
        if status == 202:
            return None
        if status >= 400:
            raise HaMcpError(f"HTTP {status}")
        text = body.decode("utf-8", errors="replace")
        if "text/event-stream" in headers.get("content-type", ""):
            candidates = parse_sse(text)
        else:
            try:
                candidates = [json.loads(text)]
            except ValueError:
                raise HaMcpError("non-JSON response") from None
        request_id = message.get("id")
        if request_id is None:
            return None
        for candidate in candidates:
            if (
                isinstance(candidate, dict)
                and candidate.get("id") == request_id
                and ("result" in candidate or "error" in candidate)
            ):
                if method == "initialize" and isinstance(candidate.get("result"), dict):
                    self._protocol_version = candidate["result"].get("protocolVersion") or None
                return candidate
        raise HaMcpError("no response for request id")

    async def _post(self, method: str, message: Dict[str, Any]):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        started = time.monotonic()
        first: Optional[float] = None
        chunks: List[bytes] = []
        try:
            request = self._client.build_request("POST", self._url, content=json.dumps(message), headers=headers)
            resp = await self._client.send(request, stream=True)
            try:
                async for chunk in resp.aiter_bytes():
                    if first is None:
                        first = time.monotonic()
                    chunks.append(chunk)
            finally:
                await resp.aclose()
        except httpx.HTTPError as e:
            raise HaMcpError(type(e).__name__) from None
        done = time.monotonic()
        body = b"".join(chunks)
        self.timings[method] = Timing(
            status=resp.status_code,
            first_byte_ms=round(((first or done) - started) * 1000),
            total_ms=round((done - started) * 1000),
            size=len(body),
        )
        return resp.status_code, resp.headers, body
