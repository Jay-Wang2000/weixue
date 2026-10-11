"""Wires the store, HA gateway, model client, engine, outbox and monitor into one service."""
import asyncio
import contextlib
import logging
import os
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Dict, Optional

import anthropic

from weixue_hamcp import HaMcpClient

from . import jsonlog, recovery
from .chats import ChatIndex
from .config import NAMESPACE_CHECK_S, Settings
from .deliveries import Outbox
from .ha_adapter import HaGateway, tunnel_iface_present
from .home import HomeSnapshot
from .monitor import Monitor
from .sse import EventHub
from .store import Store
from .turns import Engine

VERSIONS = {name: version(name) for name in ("anthropic", "mcp")}


class _RetryLog(logging.Handler):
    """SDK retry lines (`anthropic` logger) as redacted JSON lines."""

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "etrying" in message:
            jsonlog.log("api retry", level="warning", event="api_retry", detail=jsonlog.truncate(message, 160))


def install_sdk_logging() -> None:
    for name in ("anthropic", "httpx", "httpx2"):
        logger = logging.getLogger(name)
        logger.handlers = [_RetryLog()] if name == "anthropic" else []
        logger.setLevel(logging.INFO if name == "anthropic" else logging.WARNING)
        logger.propagate = False


class Service:
    def __init__(self, settings: Settings, client: Optional[Any] = None, ha_client: Optional[HaMcpClient] = None,
                 require_iface: Optional[str] = None):
        self.settings = settings
        self.store = Store(settings.db_path)
        self.hub = EventHub(self.store)
        self.outbox = Outbox(self.store)
        self.chats = ChatIndex(self.store, on_change=self._chats_changed)
        self.client = client if client is not None else anthropic.AsyncAnthropic()
        if ha_client is None and settings.ha_mcp_url:
            ha_client = HaMcpClient(settings.ha_mcp_url, timeout_s=15)
        self.ha = HaGateway(settings, self.store, ha_client, require_iface=require_iface)
        self.engine = Engine(settings, self.store, self.hub, self.client, self.ha, self.outbox, self.chats)
        self.monitor = Monitor(self.store, self.ha, self.engine, self.outbox, settings.mcp_endpoint_host,
                               probe_s=settings.probe_s, probe_timeout_s=settings.probe_timeout_s)
        self.home = HomeSnapshot(self.store, self.ha, settings.home_snapshot_s, settings.home_entities)
        self.recovery_summary: Dict[str, Any] = {}
        self._chats_pending: set = set()
        self._watch: Optional[asyncio.Task] = None

    async def start(self, background: bool = True) -> None:
        self.settings.memory_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(self.settings.memory_dir, 0o700)
        if background:
            await self.ha.start()
        else:
            self.ha.load_cache()
        self.recovery_summary = await recovery.recover(self.engine)
        if background:
            self.monitor.start()
            self.home.start()
            self._watch = asyncio.create_task(self._namespace_watch(), name="namespace-watch")
            asyncio.create_task(self.monitor.probe_all(), name="first-probe")
        jsonlog.log("service started", event="start", detail=f"anthropic {VERSIONS['anthropic']} mcp {VERSIONS['mcp']}",
                    outcome=self.store.journal_mode())

    async def stop(self) -> None:
        if self._watch:
            self._watch.cancel()
        await self.monitor.stop()
        await self.home.stop()
        await self.engine.shutdown()
        await self.ha.stop()
        with contextlib.suppress(Exception):
            await self.client.close()
        self.store.close()

    async def _namespace_watch(self) -> None:
        """Exit non-zero once eth0 leaves the namespace (the wg container was replaced)."""
        if not Path("/sys/class/net/eth0").exists():
            return
        while True:
            await asyncio.sleep(NAMESPACE_CHECK_S)
            if not Path("/sys/class/net/eth0").exists():
                jsonlog.log("network namespace lost eth0; exiting", level="error", event="namespace_lost")
                os._exit(3)

    def model_ok(self) -> bool:
        """False only while the most recent model request failed."""
        return self.engine.last_api_error is None or (self.engine.last_model_ok or 0) > self.engine.last_api_error_at

    def healthy(self) -> Dict[str, Any]:
        db = self.store.writable()
        model = self.model_ok()
        ha = self.monitor.ha_healthy()
        return {"ok": db and model and ha, "db": db, "model": model, "ha_mcp": ha}

    def _chats_changed(self, device_id: str) -> None:
        if device_id in self._chats_pending:
            return
        self._chats_pending.add(device_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self._chats_pending.discard(device_id)
            return
        loop.call_later(0.5, self._emit_chats, device_id)

    def _emit_chats(self, device_id: str) -> None:
        self._chats_pending.discard(device_id)
        with contextlib.suppress(Exception):
            self.outbox.enqueue(device_id, "chats", "SILENT", "chats", self.chats.listing(device_id))

    def status(self) -> Dict[str, Any]:
        store = self.store
        engine = self.engine
        return {
            "live_clients": len(engine.actors),
            "turns_in_flight": engine.streams_in_flight(),
            "open_tasks": store.scalar("SELECT COUNT(*) FROM tasks WHERE state NOT IN ('done', 'failed', 'cancelled')"),
            "pending_deliveries": store.scalar("SELECT COUNT(*) FROM deliveries WHERE state = 'pending'"),
            "open_holds": len(self.ha.open_holds()),
            "last_api_error": engine.last_api_error,
            "last_model_ok": engine.last_model_ok,
            "actors": [{"chat_id": a.chat_id, "busy": a.current is not None, "queued": len(a.queue),
                        "turn_id": a.current.turn_id if a.current else None,
                        "mode": a.current.mode if a.current else None,
                        "idle_s": round(time.monotonic() - a.last_used, 1)} for a in engine.actors.values()],
            "ha_mcp": {"configured": self.ha.client is not None, "tool_count": len(self.ha.tools),
                       "policy_version": self.ha.policy.get("version"), "wait_seconds": self.ha.policy.get("wait_seconds"),
                       "last_ok": self.ha.last_ok, "last_ping_ok": self.ha.last_ping_ok,
                       "last_error": self.ha.last_error, "pending_count": self.ha.last_pending_count,
                       "server": self.ha.client.server_info.get("version") if self.ha.client else None,
                       "tunnel_required": self.ha.require_iface},
            "probes": self.monitor.latest, "journal_mode": store.journal_mode(),
            "versions": VERSIONS, "e2e_guard": self.settings.e2e_guard, "recovery": self.recovery_summary,
        }


def boot_tunnel_iface() -> Optional[str]:
    """wg0 is required for HA calls when the service starts inside the tunnel namespace."""
    return "wg0" if tunnel_iface_present("wg0") else None
