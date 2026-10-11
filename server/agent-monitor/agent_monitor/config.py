"""Monitor configuration from the environment. Error messages never echo a value."""
import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

DEFAULT_TZ = "Europe/Dublin"
# The agent service's ha-mcp ping period (decision 49); the ha-mcp tile turns red after period + 15 s.
DEFAULT_HA_PROBE_S = 10.0


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


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    token = env.get("MONITOR_TOKEN", "").strip()
    if token and len(token) < 32:
        raise ConfigError("MONITOR_TOKEN is shorter than 32 characters")
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
        tunnel=env.get("WG_TUNNEL", "").strip() == "1",
        home=env.get("MONITOR_HOME", "1").strip() != "0",
        tz=tz if tz and tz != "UTC" else DEFAULT_TZ,
        ha_probe_s=ha_probe_s,
        host=env.get("MONITOR_HOST", "0.0.0.0"),
        port=port,
    )
