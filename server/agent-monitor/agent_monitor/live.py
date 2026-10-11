"""Live evidence: agent /healthz and /v1/status, the gateway's device summary, an endpoint TCP connect and the
bridge status file. Only allowlisted fields are kept."""
import asyncio
import contextlib
import json
import os
import time
from typing import Any, Callable, Dict, Optional

import httpx

from .config import Settings
from .queries import scrub

REQUEST_TIMEOUT_S = 2.0
PROBE_DETAIL_KEYS = ("tool_count", "policy_version", "wait_seconds", "pending_count", "pending_not_ours", "error",
                     "actors", "turns_in_flight", "rx_bytes", "tx_bytes")
HA_STATUS_KEYS = ("configured", "tool_count", "policy_version", "wait_seconds", "pending_count", "last_ok",
                  "last_ping_ok", "server", "tunnel_required")
DEVICE_KEYS = ("session_open", "last_poll_seconds_ago", "poll_held", "last_poll_end_seconds_ago", "reachable")


def _probes(raw: Any) -> Dict[str, Dict[str, Any]]:
    out = {}
    for name, probe in (raw or {}).items():
        if not isinstance(probe, dict) or name not in ("ha_mcp", "mcp_endpoint", "tunnel", "model_api"):
            continue
        entry = {"ok": bool(probe.get("ok")), "latency_ms": probe.get("latency_ms"), "ts": probe.get("ts")}
        for key in PROBE_DETAIL_KEYS:
            if key in probe:
                entry[key] = scrub(probe[key], 80) if isinstance(probe[key], str) else probe[key]
        out[name] = entry
    return out


def agent_status(body: Dict[str, Any]) -> Dict[str, Any]:
    ha = body.get("ha_mcp") or {}
    return {
        "open_tasks": body.get("open_tasks"),
        "turns_in_flight": body.get("turns_in_flight"),
        "open_holds": body.get("open_holds"),
        "journal_mode": body.get("journal_mode"),
        "actors": [{k: a.get(k) for k in ("chat_id", "busy", "queued", "mode", "idle_s")}
                   for a in body.get("actors") or [] if isinstance(a, dict)],
        "ha_mcp": {k: ha.get(k) for k in HA_STATUS_KEYS},
        "probes": _probes(body.get("probes")),
    }


def gateway_devices(body: Dict[str, Any]) -> Dict[str, Any]:
    devices = body.get("devices") or {}
    return {"devices": {str(d)[:64]: {k: v.get(k) for k in DEVICE_KEYS}
                        for d, v in devices.items() if isinstance(v, dict)}}


class Source:
    """The latest result of one live check, its time and the consecutive failure count."""

    def __init__(self) -> None:
        self.data: Optional[Dict[str, Any]] = None
        self.at: Optional[float] = None
        self.failures = 0
        self.error: Optional[str] = None

    def ok(self, data: Dict[str, Any], now: float) -> None:
        self.data, self.at, self.failures, self.error = data, now, 0, None

    def fail(self, error: str, now: float) -> None:
        self.failures += 1
        self.error = error
        if self.at is None:
            self.at = now

    def age(self, now: float) -> Optional[float]:
        return None if self.at is None else now - self.at


class LiveSources:
    def __init__(self, settings: Settings, client: Optional[httpx.AsyncClient] = None,
                 clock: Callable[[], float] = time.time):
        self.settings = settings
        self.clock = clock
        self.client = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
        self.health = Source()
        self.status = Source()
        self.gateway = Source()
        self.endpoint = Source()
        self.bridge = Source()

    async def aclose(self) -> None:
        await self.client.aclose()

    def _auth(self) -> Dict[str, str]:
        token = self.settings.monitor_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _get_json(self, url: str, auth: bool) -> Dict[str, Any]:
        resp = await self.client.get(url, headers=self._auth() if auth else None, timeout=REQUEST_TIMEOUT_S)
        if resp.status_code not in (200, 503):
            raise RuntimeError(f"http_{resp.status_code}")
        body = resp.json()
        if not isinstance(body, dict):
            raise RuntimeError("not_json_object")
        return body

    async def _check(self, source: Source, coro, shape: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        try:
            body = await coro
            source.ok(shape(body), self.clock())
        except Exception as e:
            source.fail(_error_code(e), self.clock())

    async def _tcp(self) -> Dict[str, Any]:
        host, _, port = self.settings.mcp_endpoint.rpartition(":")
        started = time.monotonic()
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), REQUEST_TIMEOUT_S)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return {"latency_ms": int((time.monotonic() - started) * 1000)}

    def read_bridge(self) -> None:
        path = self.settings.bridge_status
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("shape")
            data["file_mtime"] = os.stat(path).st_mtime
            self.bridge.ok({k: v for k, v in data.items() if not isinstance(v, (dict, list))}, self.clock())
        except FileNotFoundError:
            self.bridge.data = None
            self.bridge.fail("no_status_file", self.clock())
        except (OSError, ValueError) as e:
            self.bridge.data = None
            self.bridge.fail(type(e).__name__, self.clock())

    async def refresh(self) -> None:
        s = self.settings
        checks = [
            self._check(self.health, self._get_json(f"{s.agent_url}/healthz", False), lambda b: {
                k: b.get(k) for k in ("ok", "db", "model", "ha_mcp")}),
            self._check(self.status, self._get_json(f"{s.agent_url}/v1/status", True), agent_status),
            self._check(self.gateway, self._get_json(f"{s.gateway_url}/weixue/proactive", True), gateway_devices),
        ]
        if s.mcp_endpoint:
            checks.append(self._check(self.endpoint, self._tcp(), lambda b: b))
        await asyncio.gather(*checks)
        self.read_bridge()


def _error_code(e: BaseException) -> str:
    if isinstance(e, RuntimeError) and str(e).startswith(("http_", "not_json")):
        return str(e)
    if isinstance(e, (asyncio.TimeoutError, httpx.TimeoutException)):
        return "timeout"
    return type(e).__name__
