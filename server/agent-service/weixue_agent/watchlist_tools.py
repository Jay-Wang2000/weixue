"""The home media watchlist's MCP tools (taste, picks, storage, interest proposals, feedback).

Each call opens a short-lived MCP session to the watchlist's stateless streamable HTTP endpoint; the tool list is
refreshed in the background and offered to the model with deferred loading.
"""
import asyncio
import contextlib
from typing import Any, AsyncIterator, Dict, List, Optional

from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import CallToolResult, TextContent, Tool

from . import jsonlog

TOOL_PREFIX = "watchlist_"
CALL_TIMEOUT_S = 20
REFRESH_S = 900
RETRY_S = 60
UNREACHABLE_TEXT = "The watchlist service is unreachable right now."


class WatchlistTools:
    def __init__(self, url: Optional[str], token: Optional[str]):
        self.url = url
        self.token = token
        self.tools: List[Tool] = []
        self.last_error: Optional[str] = None
        self._loop: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.token)

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[ClientSession]:
        http = create_mcp_http_client(headers={"Authorization": f"Bearer {self.token}"})
        async with http, streamable_http_client(self.url, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session

    async def refresh(self) -> None:
        async def fetch() -> List[Tool]:
            async with self._session() as session:
                return list((await session.list_tools()).tools)

        listed = await asyncio.wait_for(fetch(), timeout=CALL_TIMEOUT_S)
        names = [t.name for t in self.tools]
        self.tools = [t for t in listed if t.name.startswith(TOOL_PREFIX)]
        self.last_error = None
        if [t.name for t in self.tools] != names:
            jsonlog.log("watchlist tools refreshed", event="watchlist_tools", outcome=str(len(self.tools)))

    async def _refresh_loop(self) -> None:
        while True:
            try:
                await self.refresh()
                delay = REFRESH_S
            except Exception as e:
                self.last_error = type(e).__name__
                jsonlog.log("watchlist tools refresh failed", level="warning", code=self.last_error)
                delay = RETRY_S
            await asyncio.sleep(delay)

    def start(self) -> None:
        if self.enabled and self._loop is None:
            self._loop = asyncio.create_task(self._refresh_loop(), name="watchlist-refresh")

    async def stop(self) -> None:
        if self._loop is not None:
            self._loop.cancel()
            with contextlib.suppress(BaseException):
                await self._loop
            self._loop = None

    def mcp_tools(self) -> List[Tool]:
        out = []
        for tool in self.tools:
            spec = {k: v for k, v in tool.model_dump(by_alias=True, exclude_none=True).items()
                    if k not in ("outputSchema", "icons", "execution", "_meta")}
            with contextlib.suppress(Exception):
                out.append(Tool.model_validate(spec))
        return out

    async def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None, **_: Any) -> CallToolResult:
        """The `ClientSession`-shaped call `async_mcp_tool` makes; failures come back as error results."""
        async def call() -> CallToolResult:
            async with self._session() as session:
                return await session.call_tool(name, dict(arguments or {}))

        try:
            return await asyncio.wait_for(call(), timeout=CALL_TIMEOUT_S)
        except Exception as e:
            jsonlog.log("watchlist tool call failed", level="warning", code=type(e).__name__, detail=name)
            return CallToolResult(content=[TextContent(type="text", text=UNREACHABLE_TEXT)], isError=True)
