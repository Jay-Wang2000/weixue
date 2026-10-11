"""Connection tiles: ok (green), warn (amber), bad (red), off (hidden), from live evidence and its age."""
from typing import Any, Dict, List, Optional

DEVICE_POLL_GAP_S = 3
AGENT_FAILS_RED = 2
HA_FIRST_BYTE_WARN_MS = 2000
HA_AGE_MARGIN_S = 15
BRIDGE_HEARTBEAT_S = 30
BRIDGE_MAX_AGE_S = 2 * BRIDGE_HEARTBEAT_S + 5
TOOLS_STALE_S = 1800
ENDPOINT_FAILS_RED = 2

WORDS = {"ok": "ok", "warn": "degraded", "bad": "down", "unknown": "unknown", "off": "off"}


def tile(tile_id: str, label: str, state: str, age_s: Optional[float] = None, code: Optional[str] = None,
         value: Optional[str] = None, word: Optional[str] = None, **detail: Any) -> Dict[str, Any]:
    return {"id": tile_id, "label": label, "state": state, "word": word or WORDS[state],
            "age_s": None if age_s is None else round(max(0.0, age_s), 1), "code": code, "value": value,
            "detail": detail}


def worst(states: List[str]) -> str:
    order = ["bad", "unknown", "warn", "ok"]
    return min(states, key=order.index) if states else "unknown"


def device(gateway: Optional[Dict[str, Any]], gateway_error: Optional[str], gateway_age_s: Optional[float]) -> Dict[str, Any]:
    """Green while a WebSocket session is open or a long-poll is held; red DEVICE_POLL_GAP_S after the last poll
    ended with none held, or while the gateway cannot be read."""
    if gateway is None:
        return tile("device", "Device", "bad", gateway_age_s, code=gateway_error or "gateway_unread")
    devices = gateway.get("devices") or {}
    if not devices:
        return tile("device", "Device", "bad", None, code="no_device_seen")
    rows, states = [], []
    for device_id, d in sorted(devices.items()):
        if d.get("session_open"):
            state, word = "ok", "session"
        elif d.get("poll_held"):
            state, word = "ok", "polling"
        else:
            gap = d.get("last_poll_end_seconds_ago")
            state = "ok" if gap is not None and gap <= DEVICE_POLL_GAP_S else "bad"
            word = "polling" if state == "ok" else "offline"
        states.append(state)
        rows.append({"device_id": device_id, "state": state, "word": word,
                     "last_poll_seconds_ago": d.get("last_poll_seconds_ago"),
                     "last_poll_end_seconds_ago": d.get("last_poll_end_seconds_ago")})
    state = worst(states)
    first = next(r for r in rows if r["state"] == state)
    age = first["last_poll_end_seconds_ago"] if first["word"] == "offline" else 0.0
    return tile("device", "Device", state, age, code="no_session_or_poll" if state == "bad" else None,
                word=first["word"] if state == "ok" else "offline", devices=rows)


def agent(health: Optional[Dict[str, Any]], failures: int, error: Optional[str], age_s: Optional[float]) -> Dict[str, Any]:
    """Red after AGENT_FAILS_RED consecutive failed /healthz requests or a database failure; amber while it
    answers not-ok (model or ha-mcp check failing)."""
    if failures >= AGENT_FAILS_RED or health is None:
        return tile("agent", "Agent", "bad" if failures >= AGENT_FAILS_RED else "unknown", age_s,
                    code=error or "unreachable")
    if health.get("ok"):
        return tile("agent", "Agent", "ok", age_s, **_flags(health))
    if health.get("db") is False:
        return tile("agent", "Agent", "bad", age_s, code="db", **_flags(health))
    failing = [k for k in ("model", "ha_mcp") if health.get(k) is False]
    return tile("agent", "Agent", "warn", age_s, code=",".join(failing) or "not_ok", **_flags(health))


def _flags(health: Dict[str, Any]) -> Dict[str, Any]:
    return {k: health.get(k) for k in ("db", "model", "ha_mcp") if k in health}


def ha_mcp(probe: Optional[Dict[str, Any]], now: float, probe_s: float,
           tools_age_s: Optional[float] = None) -> Dict[str, Any]:
    """Red when the latest ping failed or is older than probe_s + HA_AGE_MARGIN_S; amber when the first byte
    took longer than HA_FIRST_BYTE_WARN_MS or the tool list is older than TOOLS_STALE_S."""
    limit = probe_s + HA_AGE_MARGIN_S
    if probe is None or probe.get("ts") is None:
        return tile("ha_mcp", "ha-mcp", "bad", None, code="no_probe")
    age = now - probe["ts"]
    latency = probe.get("latency_ms")
    value = f"{latency} ms" if latency is not None else None
    if not probe.get("ok"):
        return tile("ha_mcp", "ha-mcp", "bad", age, code=probe.get("error") or "ping_failed", value=value)
    if age > limit:
        return tile("ha_mcp", "ha-mcp", "bad", age, code="stale", value=value, word="stale")
    if latency is not None and latency > HA_FIRST_BYTE_WARN_MS:
        return tile("ha_mcp", "ha-mcp", "warn", age, code="slow", value=value)
    if tools_age_s is not None and tools_age_s > TOOLS_STALE_S:
        return tile("ha_mcp", "ha-mcp", "warn", age, code="tools_stale", value=value)
    return tile("ha_mcp", "ha-mcp", "ok", age, value=value)


def bridge(status: Optional[Dict[str, Any]], now: float, error: Optional[str] = None) -> Dict[str, Any]:
    """Red when the status file is missing, older than BRIDGE_MAX_AGE_S or not `connected`; amber while the
    bridge serves its cached tool list or has not announced."""
    if status is None:
        return tile("bridge", "Bridge", "bad", None, code=error or "no_status_file")
    age = now - float(status.get("ts") or 0)
    p50 = status.get("ping_first_byte_ms_p50")
    value = f"{p50} ms" if p50 is not None else None
    extra = {k: status.get(k) for k in ("tools", "tools_age_s", "tools_source", "sessions_opened", "session",
                                        "ping_first_byte_ms_max", "announced")}
    if age > BRIDGE_MAX_AGE_S:
        return tile("bridge", "Bridge", "bad", age, code="stale", value=value, word="stale", **extra)
    if status.get("state") != "connected":
        return tile("bridge", "Bridge", "bad", age, code=status.get("state") or "unknown", value=value, **extra)
    if status.get("tools_source") == "cache" or not status.get("announced"):
        code = "cached_tools" if status.get("tools_source") == "cache" else "not_announced"
        return tile("bridge", "Bridge", "warn", age, code=code, value=value, **extra)
    return tile("bridge", "Bridge", "ok", age, value=value, **extra)


def endpoint(failures: int, latency_ms: Optional[int], error: Optional[str], age_s: Optional[float],
             enabled: bool = True) -> Dict[str, Any]:
    if not enabled:
        return tile("endpoint", "Endpoint", "off")
    if failures >= ENDPOINT_FAILS_RED:
        return tile("endpoint", "Endpoint", "bad", age_s, code=error or "unreachable")
    if latency_ms is None:
        return tile("endpoint", "Endpoint", "unknown", age_s, code=error)
    return tile("endpoint", "Endpoint", "ok", age_s, value=f"{latency_ms} ms")


def tunnel(enabled: bool, probe: Optional[Dict[str, Any]], now: float, probe_s: float) -> Dict[str, Any]:
    if not enabled:
        return tile("tunnel", "Tunnel", "off")
    if probe is None or probe.get("ts") is None:
        return tile("tunnel", "Tunnel", "bad", None, code="no_probe")
    age = now - probe["ts"]
    if not probe.get("ok"):
        return tile("tunnel", "Tunnel", "bad", age, code="down")
    if age > probe_s + HA_AGE_MARGIN_S:
        return tile("tunnel", "Tunnel", "bad", age, code="stale", word="stale")
    return tile("tunnel", "Tunnel", "ok", age)
