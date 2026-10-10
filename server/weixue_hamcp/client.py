"""Streamable HTTP client for ha-mcp and its policy HTTP API: one POST per JSON-RPC message, one MCP
session per process (opened lazily, ended with DELETE, reopened once after a 404).

Errors never carry the URL.
"""
import asyncio
import itertools
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx

CONNECT_TIMEOUT_S = 5
INIT_TIMEOUT_S = 15
TOOLS_LIST_TIMEOUT_S = 20
PING_TIMEOUT_S = 10
TERMINATE_TIMEOUT_S = 5
POLICY_TIMEOUT_S = 15
PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "weixue-agent", "version": "1"}
# Whole-exchange bounds, headers through the last body byte; a tool call passes its own.
METHOD_TIMEOUT_S = {
    "initialize": INIT_TIMEOUT_S,
    "notifications/initialized": INIT_TIMEOUT_S,
    "tools/list": TOOLS_LIST_TIMEOUT_S,
    "ping": PING_TIMEOUT_S,
}
SESSION_EXPIRED = "HTTP 404 (session expired)"


class HaMcpError(Exception):
    """Transport or protocol failure talking to ha-mcp."""


class PolicyError(Exception):
    def __init__(self, status: int, body: Dict[str, Any]):
        super().__init__(f"policy API HTTP {status}")
        self.status = status
        self.body = body


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
    def __init__(self, url: str, transport: Optional[httpx.AsyncBaseTransport] = None, timeout_s: float = 120):
        self._url = url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout_s, connect=CONNECT_TIMEOUT_S), transport=transport)
        self._ids = itertools.count(1)
        self._protocol_version: Optional[str] = None
        self._session_id: Optional[str] = None
        self._init_lock = asyncio.Lock()
        self.init_result: Optional[Dict[str, Any]] = None
        self.server_info: Dict[str, Any] = {}
        self.sessions_opened = 0
        self.timings: Dict[str, Timing] = {}

    @property
    def session(self) -> str:
        return session_label(self._session_id)

    async def aclose(self) -> None:
        await self.terminate()
        await self._client.aclose()

    async def ensure_session(self) -> Dict[str, Any]:
        """The initialize result of the held session; opens one (initialize + initialized) when none is held."""
        async with self._init_lock:
            if self.init_result is not None:
                return self.init_result
            await self.terminate()
            resp = await self.send({
                "jsonrpc": "2.0", "id": next(self._ids), "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            })
            result = (resp or {}).get("result")
            if not isinstance(result, dict):
                raise HaMcpError("initialize returned an error")
            await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.init_result = result
            self.server_info = result.get("serverInfo") or {}
            self.sessions_opened += 1
            return result

    async def initialize(self) -> Dict[str, Any]:
        return await self.ensure_session()

    async def terminate(self) -> None:
        """End the held session with a DELETE carrying its Mcp-Session-Id; a stateless session has nothing to end."""
        session_id, version = self._session_id, self._protocol_version
        self._session_id = None
        self._protocol_version = None
        self.init_result = None
        if not session_id:
            return
        headers = {"Mcp-Session-Id": session_id}
        if version:
            headers["MCP-Protocol-Version"] = version
        try:
            await self._client.delete(self._url, headers=headers,
                                      timeout=httpx.Timeout(TERMINATE_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
        except httpx.HTTPError:
            pass

    async def send(self, message: Dict[str, Any], read_timeout_s: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """POST one message; the matching response for a request, None for a notification. The whole exchange
        is bounded by read_timeout_s, else by METHOD_TIMEOUT_S for its method."""
        method = message.get("method") if isinstance(message.get("method"), str) else ""
        bound = read_timeout_s if read_timeout_s is not None else METHOD_TIMEOUT_S.get(method, 120)
        try:
            status, headers, body = await asyncio.wait_for(self._post(method, message), bound)
        except asyncio.TimeoutError:
            raise HaMcpError("ReadTimeout") from None
        if status == 404 and self._session_id:
            self._session_id = None
            self._protocol_version = None
            self.init_result = None
            raise HaMcpError(SESSION_EXPIRED)
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
            if (isinstance(candidate, dict) and candidate.get("id") == request_id
                    and ("result" in candidate or "error" in candidate)):
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
        self.timings[method] = Timing(status=resp.status_code, first_byte_ms=round(((first or done) - started) * 1000),
                                      total_ms=round((done - started) * 1000), size=len(body))
        return resp.status_code, resp.headers, body

    async def request(self, method: str, params: Dict[str, Any], read_timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """Result of one JSON-RPC request; HaMcpError on a transport failure, an RPC error or no result."""
        await self.ensure_session()
        resp = await self.send({"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params},
                               read_timeout_s=read_timeout_s)
        if resp is None:
            raise HaMcpError("no response")
        if "error" in resp:
            raise HaMcpError(f"JSON-RPC error {(resp.get('error') or {}).get('code')}")
        result = resp.get("result")
        if not isinstance(result, dict):
            raise HaMcpError("no result")
        return result

    async def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(20):
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any], read_timeout_s: float) -> Dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments}, read_timeout_s)

    async def _policy(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        try:
            resp = await self._client.request(method, f"{self._url}/api/policy/{path}", json=body,
                                              timeout=httpx.Timeout(POLICY_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
        except httpx.HTTPError as e:
            raise HaMcpError(type(e).__name__) from None
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code >= 400:
            raise PolicyError(resp.status_code, data if isinstance(data, dict) else {})
        return data if isinstance(data, dict) else {}

    async def policy_config(self) -> Dict[str, Any]:
        return await self._policy("GET", "config")

    async def pending(self) -> List[Dict[str, Any]]:
        return list((await self._policy("GET", "pending")).get("pending") or [])

    async def approve(self, token: str) -> Dict[str, Any]:
        return await self._policy("POST", "approve", {"token": token})

    async def deny(self, token: str) -> Dict[str, Any]:
        return await self._policy("POST", "deny", {"token": token})
