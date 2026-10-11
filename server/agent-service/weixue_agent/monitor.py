"""Periodic health probes, outbox sweeps, idle-actor eviction and retention pruning."""
import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any, Dict, Optional

from . import jsonlog
from .config import (ACTOR_IDLE_S, EVENT_RETENTION_DAYS, HA_PING_MAX_AGE_S, HEALTH_PROBE_S, HEALTH_PROBE_TIMEOUT_S,
                     PROBE_ROW_EVERY_S)
from .deliveries import Outbox
from .ha_adapter import HaGateway
from .store import Store

PRUNE_EVERY_S = 6 * 3600


async def probe_tcp(hostport: str, timeout: float = 3.0) -> Dict[str, Any]:
    host, _, port = hostport.rpartition(":")
    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout=timeout)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return {"ok": True, "latency_ms": int((time.monotonic() - started) * 1000)}
    except Exception as e:
        return {"ok": False, "latency_ms": None, "error": type(e).__name__}


def interface_counters(name: str = "wg0") -> Optional[Dict[str, Any]]:
    """Counters of a network interface in this namespace, None when it is absent."""
    base = Path("/sys/class/net") / name
    if not base.exists():
        return None
    stats = {}
    for counter in ("rx_bytes", "tx_bytes"):
        with contextlib.suppress(OSError, ValueError):
            stats[counter] = int((base / "statistics" / counter).read_text())
    return stats


class Monitor:
    def __init__(self, store: Store, ha: HaGateway, engine: Any, outbox: Outbox, mcp_endpoint: Optional[str],
                 probe_s: float = HEALTH_PROBE_S, probe_timeout_s: float = HEALTH_PROBE_TIMEOUT_S,
                 clock=time.monotonic):
        self.store = store
        self.ha = ha
        self.engine = engine
        self.outbox = outbox
        self.mcp_endpoint = mcp_endpoint
        self.probe_s = probe_s
        self.probe_timeout_s = probe_timeout_s
        self.clock = clock
        self.latest: Dict[str, Dict[str, Any]] = {}
        self._row_written: Dict[str, tuple] = {}
        self._task: Optional[asyncio.Task] = None
        self._last_prune = 0.0

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="monitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    async def _loop(self) -> None:
        while True:
            started = time.monotonic()
            with contextlib.suppress(Exception):
                await self.probe_all()
            with contextlib.suppress(Exception):
                self.outbox.sweep()
            with contextlib.suppress(Exception):
                self.engine.reap_idle(ACTOR_IDLE_S)
            if time.monotonic() - self._last_prune > PRUNE_EVERY_S:
                self._last_prune = time.monotonic()
                with contextlib.suppress(Exception):
                    removed = self.store.prune(EVENT_RETENTION_DAYS)
                    jsonlog.log("retention prune", event="prune", detail=str(removed))
            await asyncio.sleep(max(0.0, self.probe_s - (time.monotonic() - started)))

    def _record(self, probe: str, ok: bool, latency_ms: Optional[int], detail: Dict[str, Any]) -> None:
        """Every result goes to `latest`; a health_probes row only when ok flips or PROBE_ROW_EVERY_S passed."""
        self.latest[probe] = {"ok": ok, "latency_ms": latency_ms, "ts": time.time(), **detail}
        now = self.clock()
        last = self._row_written.get(probe)
        if last is None or last[0] != ok or now - last[1] >= PROBE_ROW_EVERY_S:
            self.store.record_probe(probe, ok, latency_ms, detail)
            self._row_written[probe] = (ok, now)

    async def probe_all(self) -> None:
        if self.ha.client is not None:
            started = time.monotonic()
            ok = await self.ha.ping(timeout_s=self.probe_timeout_s)
            latency = int((time.monotonic() - started) * 1000) if ok else None
            detail: Dict[str, Any] = {"tool_count": len(self.ha.tools), "policy_version": self.ha.policy.get("version"),
                                      "wait_seconds": self.ha.policy.get("wait_seconds")}
            if ok:
                with contextlib.suppress(Exception):
                    pending = await self.ha.poll_once()
                    detail["pending_count"] = len(pending)
                    ours = {r["ha_token"] for r in self.store.all("SELECT ha_token FROM approvals WHERE ha_token IS NOT NULL")}
                    detail["pending_not_ours"] = sum(1 for p in pending if p.get("token") not in ours)
            else:
                detail["error"] = jsonlog.truncate(self.ha.last_error or "ping failed", 80)
            self._record("ha_mcp", ok, latency, detail)
        else:
            self._record("ha_mcp", False, None, {"error": "HA_MCP_URL unset"})
        if self.mcp_endpoint:
            result = await probe_tcp(self.mcp_endpoint)
            self._record("mcp_endpoint", result["ok"], result["latency_ms"],
                         {k: v for k, v in result.items() if k == "error"})
        tunnel = interface_counters("wg0")
        if tunnel is not None:
            self._record("tunnel", self.latest.get("ha_mcp", {}).get("ok", False), None,
                         {**tunnel, "handshake_age_s": None})
        self._record("model_api", self.engine.last_api_error is None or
                     (self.engine.last_model_ok or 0) > self.engine.last_api_error_at, None, {
                         "actors": len(self.engine.actors), "turns_in_flight": self.engine.streams_in_flight(),
                         "last_model_ok": self.engine.last_model_ok, "last_api_error": self.engine.last_api_error})

    def ha_healthy(self) -> bool:
        if self.ha.client is None:
            return False
        return self.ha.last_ping_ok is not None and time.time() - self.ha.last_ping_ok <= HA_PING_MAX_AGE_S
