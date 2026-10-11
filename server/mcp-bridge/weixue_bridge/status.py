"""Status file for the monitor board: written atomically (temp file + rename), never a URL or session id."""
import json
import logging
import os
import statistics
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

from .hamcp import session_label

log = logging.getLogger("weixue_bridge")

PING_WINDOW = 10


class BridgeStatus:
    """state: starting | connected | disconnected | stopped."""

    def __init__(self, path: str, ha: Any = None, catalog: Any = None, clock: Callable[[], float] = time.time):
        self.path = path
        self.ha = ha
        self.catalog = catalog
        self.clock = clock
        self.state = "starting"
        self.announced = False
        self.last_ping_ok_at: Optional[float] = None
        self.ping_ms: Deque[int] = deque(maxlen=PING_WINDOW)

    def snapshot(self) -> Dict[str, Any]:
        now = self.clock()
        ha, catalog = self.ha, self.catalog
        tools_list = ha.timings.get("tools/list") if ha is not None else None
        tools = catalog.tools if catalog is not None else None
        fetched = catalog.fetched_at if catalog is not None else 0
        return {
            "ts": round(now, 3),
            "state": self.state,
            "announced": self.announced,
            "last_ping_ok_at": round(self.last_ping_ok_at, 3) if self.last_ping_ok_at else None,
            "session": session_label(ha.session_id) if ha is not None else None,
            "sessions_opened": ha.sessions_opened if ha is not None else 0,
            "ping_first_byte_ms_p50": round(statistics.median(self.ping_ms)) if self.ping_ms else None,
            "ping_first_byte_ms_max": max(self.ping_ms) if self.ping_ms else None,
            "tools_list_first_byte_ms": tools_list.first_byte_ms if tools_list else None,
            "tools_list_total_ms": tools_list.total_ms if tools_list else None,
            "tools": len(tools) if tools is not None else None,
            "tools_age_s": round(now - fetched) if fetched else None,
            "tools_source": getattr(catalog, "source", None),
        }

    def update(self, **changes: Any) -> None:
        for key, value in changes.items():
            setattr(self, key, value)
        self.write()

    def ping(self, ok: bool, first_byte_ms: Optional[int]) -> None:
        if ok:
            self.last_ping_ok_at = self.clock()
            if first_byte_ms is not None:
                self.ping_ms.append(first_byte_ms)
        self.write()

    def write(self) -> None:
        if not self.path:
            return
        tmp = f"{self.path}.tmp"
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.snapshot(), f, separators=(",", ":"))
            os.chmod(tmp, 0o644)
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("status file not written: %s", type(e).__name__)
