"""ha-mcp's initialize result and unfiltered tool list, cached on disk and refreshed in the background."""
import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .hamcp import HaMcpClient, HaMcpError, session_label

TOOLS_REFRESH_S = 900
TOOLS_RETRY_MIN_S = 5
TOOLS_RETRY_MAX_S = 300
MAX_PAGES = 20
TOOLS_ID = "weixue-bridge-tools-refresh"

log = logging.getLogger("weixue_bridge")
# Module-level so it can be replaced independently of asyncio.sleep.
_sleep = asyncio.sleep


class ToolCatalog:
    def __init__(self, ha: HaMcpClient, path: str = ""):
        self._ha = ha
        self._path = path
        self.init_result: Optional[Dict[str, Any]] = None
        self.tools: Optional[List[Dict[str, Any]]] = None
        self.fetched_at = 0.0
        # "cache" while the list comes from the cache file, "live" after a fetch in this process.
        self.source: Optional[str] = None
        self.ready = asyncio.Event()
        self._inflight: Optional[asyncio.Task] = None

    def load(self) -> bool:
        """Adopts the cache file when it holds an initialize result and a tool list."""
        if not self._path:
            return False
        try:
            with open(self._path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return False
        except (OSError, ValueError) as e:
            log.warning("tool cache unreadable: %s", type(e).__name__)
            return False
        if not (
            isinstance(data, dict)
            and isinstance(data.get("initialize"), dict)
            and isinstance(data.get("tools"), list)
            and isinstance(data.get("fetched_at"), (int, float))
        ):
            log.warning("tool cache ignored: unexpected shape")
            return False
        self.init_result = data["initialize"]
        self.tools = data["tools"]
        self.fetched_at = float(data["fetched_at"])
        self.source = "cache"
        self.ready.set()
        log.info("tool cache loaded: %d tools, age_s=%d", len(self.tools), max(0, time.time() - self.fetched_at))
        return True

    def _save(self) -> None:
        if not self._path:
            return
        tmp = f"{self._path}.tmp"
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"fetched_at": self.fetched_at, "initialize": self.init_result, "tools": self.tools}, f)
            os.replace(tmp, self._path)
        except OSError as e:
            log.warning("tool cache not written: %s", type(e).__name__)

    async def refresh(self) -> bool:
        """Fetches initialize + tools/list (all pages) over the held session; True when the list changed.
        Concurrent callers share one fetch."""
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.ensure_future(self._fetch())
        return await asyncio.shield(self._inflight)

    async def aclose(self) -> None:
        """Cancels a fetch in flight."""
        if self._inflight is not None and not self._inflight.done():
            self._inflight.cancel()
            await asyncio.gather(self._inflight, return_exceptions=True)

    async def _fetch(self) -> bool:
        init = await self._ha.ensure_session()
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_PAGES):
            message: Dict[str, Any] = {"jsonrpc": "2.0", "id": TOOLS_ID, "method": "tools/list"}
            if cursor:
                message["params"] = {"cursor": cursor}
            response = await self._ha.send(message)
            result = response.get("result") if isinstance(response, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise HaMcpError("tools/list returned an error")
            tools.extend(t for t in result["tools"] if isinstance(t, dict))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        timing = self._ha.timings["tools/list"]
        changed = tools != self.tools or init["result"] != self.init_result
        self.init_result = init["result"]
        self.tools = tools
        self.fetched_at = time.time()
        self.source = "live"
        self._save()
        self.ready.set()
        log.info(
            "ha-mcp tools/list ok tools=%d bytes=%d first_byte_ms=%d total_ms=%d session=%s sessions_opened=%d changed=%s",
            len(tools),
            timing.size,
            timing.first_byte_ms,
            timing.total_ms,
            session_label(self._ha.session_id),
            self._ha.sessions_opened,
            "yes" if changed else "no",
        )
        return changed


async def refresh_loop(catalog: ToolCatalog, on_change: Callable[[], Awaitable[None]]) -> None:
    """Refreshes every TOOLS_REFRESH_S; after a failure retries with doubling backoff
    from TOOLS_RETRY_MIN_S to TOOLS_RETRY_MAX_S."""
    delay = TOOLS_RETRY_MIN_S
    while True:
        try:
            changed = await catalog.refresh()
        except HaMcpError as e:
            log.warning("ha-mcp tools/list refresh failed: %s; retry in %d s", e, delay)
            await _sleep(delay)
            delay = min(delay * 2, TOOLS_RETRY_MAX_S)
            continue
        delay = TOOLS_RETRY_MIN_S
        if changed:
            await on_change()
        await _sleep(TOOLS_REFRESH_S)
