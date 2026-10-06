"""Minimal Streamable HTTP client: one POST per JSON-RPC message."""
import json
from typing import Any, Dict, List, Optional

import httpx

CONNECT_TIMEOUT_S = 5
CALL_TIMEOUT_S = 120


class HaMcpError(Exception):
    """Transport failure talking to ha-mcp; the message never contains the URL."""


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


class HaMcpClient:
    def __init__(self, url: str, transport: Optional[httpx.AsyncBaseTransport] = None):
        self._url = url
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S), transport=transport
        )
        self._protocol_version: Optional[str] = None
        self._session_id: Optional[str] = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST one message; the matching response for a request, None for a notification."""
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        try:
            resp = await self._client.post(self._url, content=json.dumps(message), headers=headers)
        except httpx.HTTPError as e:
            raise HaMcpError(type(e).__name__) from None
        if resp.status_code == 404 and self._session_id:
            self._session_id = None
        if resp.headers.get("mcp-session-id"):
            self._session_id = resp.headers["mcp-session-id"]
        if resp.status_code == 202:
            return None
        if resp.status_code >= 400:
            raise HaMcpError(f"HTTP {resp.status_code}")
        if "text/event-stream" in resp.headers.get("content-type", ""):
            candidates = parse_sse(resp.text)
        else:
            try:
                candidates = [resp.json()]
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
                if message.get("method") == "initialize" and isinstance(candidate.get("result"), dict):
                    self._protocol_version = candidate["result"].get("protocolVersion") or None
                return candidate
        raise HaMcpError("no response for request id")
