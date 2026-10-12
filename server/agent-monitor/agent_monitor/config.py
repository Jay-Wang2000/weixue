"""Monitor configuration from the environment. Error messages never echo a value."""
import os
import re
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

DEFAULT_TZ = "Europe/Dublin"
# The agent service's ha-mcp ping period (decision 49); the ha-mcp tile turns red after period + 15 s.
DEFAULT_HA_PROBE_S = 10.0
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: str = "/db/agent.db"
    bridge_status: str = "/bridge/bridge.json"
    agent_url: str = "http://agent-service:8010"
    gateway_url: str = "http://xiaozhi-esp32-server:8003"
    mcp_endpoint: str = "mcp-endpoint-server:8004"
    monitor_token: str = ""
    view_token: str = ""
    local_only: bool = False
    frame_ancestors: str = "'none'"
    tunnel: bool = False
    home: bool = True
    tz: str = DEFAULT_TZ
    ha_probe_s: float = DEFAULT_HA_PROBE_S
    host: str = "0.0.0.0"
    port: int = 8020


def _base_url(raw: str, name: str) -> str:
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path not in ("", "/") or parts.query:
        raise ConfigError(f"{name} must be http(s)://<host>:<port>")
    return raw.strip().rstrip("/")


def _host_port(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    host, sep, port = value.rpartition(":")
    if not sep or not host or "/" in value or not port.isdigit():
        raise ConfigError("MCP_ENDPOINT must be <host>:<port>")
    return value


def _frame_ancestors(raw: str) -> str:
    """CSP frame-ancestors sources: 'none' (default), 'self' and http(s)://host[:port] origins."""
    sources = raw.split()
    if not sources or sources == ["'none'"]:
        return "'none'"
    if not all(s == "'self'" or _ORIGIN.match(s) for s in sources):
        raise ConfigError("MONITOR_FRAME_ANCESTORS must be 'none', 'self' or http(s)://<host>[:<port>] origins")
    return " ".join(sources)


def _token(env: Mapping[str, str], name: str) -> str:
    token = env.get(name, "").strip()
    if token and len(token) < 32:
        raise ConfigError(f"{name} is shorter than 32 characters")
    return token


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    token = _token(env, "MONITOR_TOKEN")
    view_token = _token(env, "MONITOR_VIEW_TOKEN")
    if token and view_token == token:
        raise ConfigError("MONITOR_VIEW_TOKEN must differ from MONITOR_TOKEN")
    try:
        port = int(env.get("MONITOR_PORT", "8020"))
        ha_probe_s = float(env.get("AGENT_HA_PROBE_S", "") or DEFAULT_HA_PROBE_S)
    except ValueError:
        raise ConfigError("MONITOR_PORT and AGENT_HA_PROBE_S must be numbers") from None
    tz = env.get("TZ", "").strip()
    return Settings(
        db_path=env.get("MONITOR_DB", "/db/agent.db"),
        bridge_status=env.get("BRIDGE_STATUS_FILE", "/bridge/bridge.json"),
        agent_url=_base_url(env.get("AGENT_SERVICE_URL", "http://agent-service:8010"), "AGENT_SERVICE_URL"),
        gateway_url=_base_url(env.get("GATEWAY_URL", "http://xiaozhi-esp32-server:8003"), "GATEWAY_URL"),
        mcp_endpoint=_host_port(env.get("MCP_ENDPOINT", "mcp-endpoint-server:8004")),
        monitor_token=token,
        view_token=view_token,
        local_only=env.get("MONITOR_BIND", "").strip() == "127.0.0.1",
        frame_ancestors=_frame_ancestors(env.get("MONITOR_FRAME_ANCESTORS", "")),
        tunnel=env.get("WG_TUNNEL", "").strip() == "1",
        home=env.get("MONITOR_HOME", "1").strip() != "0",
        tz=tz if tz and tz != "UTC" else DEFAULT_TZ,
        ha_probe_s=ha_probe_s,
        host=env.get("MONITOR_HOST", "0.0.0.0"),
        port=port,
    )
