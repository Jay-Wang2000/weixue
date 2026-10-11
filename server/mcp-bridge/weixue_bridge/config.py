"""Bridge configuration from the environment. Error messages never echo a URL."""
import json
import os
from dataclasses import dataclass
from typing import FrozenSet, Mapping
from urllib.parse import parse_qs, urlsplit

DEFAULT_ENDPOINT_FILE = "/run/secrets/mcp-endpoint-url"
DEFAULT_FILTER_FILE = "/app/policy/voice_bridge.json"
DEFAULT_HEALTH_FILE = "/tmp/mcp-bridge.healthy"
DEFAULT_TOOLS_CACHE = "/app/cache/ha-mcp-tools.json"
DEFAULT_STATUS_FILE = "/app/status/bridge.json"
HEARTBEAT_S = 30
HEALTH_MAX_AGE_S = 3 * HEARTBEAT_S
# Consecutive failed endpoint connects before the bridge exits.
ENDPOINT_MAX_FAILURES = 12
# Interface whose disappearance from the network namespace makes the bridge exit.
NAMESPACE_IFACE = "eth0"
IFACE_POLL_S = 2


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class VoiceFilter:
    tools: FrozenSet[str]
    call_service_domains: FrozenSet[str]


@dataclass(frozen=True)
class BridgeConfig:
    ha_mcp_url: str
    endpoint_url: str
    voice_filter: VoiceFilter
    health_file: str
    tunnel_iface: str = ""
    tools_cache: str = ""
    status_file: str = ""


def redact(url: str) -> str:
    """scheme://host:port of a URL, dropping the secret path and query."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def parse_ha_mcp_url(raw: str) -> str:
    url = raw.strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("HA_MCP_URL must be http(s)://<host>:<port>/<secret path>")
    if parts.path in ("", "/"):
        raise ConfigError("HA_MCP_URL has no secret path")
    if parts.query or parts.fragment:
        raise ConfigError("HA_MCP_URL must not carry a query or fragment")
    return url


def parse_endpoint_url(raw: str) -> str:
    url = raw.strip()
    parts = urlsplit(url)
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        raise ConfigError("MCP endpoint URL must be ws(s)://<host>:<port>/mcp_endpoint/mcp/?token=...")
    if not parts.path.endswith("/mcp/"):
        raise ConfigError("MCP endpoint URL must be the tool-side /mcp_endpoint/mcp/ path")
    if not parse_qs(parts.query).get("token", [""])[0]:
        raise ConfigError("MCP endpoint URL has no token")
    return url


def load_voice_filter(path: str) -> VoiceFilter:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"voice filter {path}: {type(e).__name__}") from None
    if not isinstance(data, dict) or set(data) != {"tools", "call_service_domains"}:
        raise ConfigError(f"voice filter {path}: keys must be exactly tools, call_service_domains")
    for key in ("tools", "call_service_domains"):
        values = data[key]
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            raise ConfigError(f"voice filter {path}: {key} must be a list of non-empty strings")
    if not data["tools"]:
        raise ConfigError(f"voice filter {path}: tools is empty")
    return VoiceFilter(
        tools=frozenset(v.strip() for v in data["tools"]),
        call_service_domains=frozenset(v.strip().lower() for v in data["call_service_domains"]),
    )


def _read_endpoint(env: Mapping[str, str]) -> str:
    if env.get("MCP_ENDPOINT", "").strip():
        return env["MCP_ENDPOINT"]
    path = env.get("MCP_ENDPOINT_FILE", DEFAULT_ENDPOINT_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        raise ConfigError(f"MCP_ENDPOINT unset and {path} unreadable: {type(e).__name__}") from None


def load_config(env: Mapping[str, str] = os.environ) -> BridgeConfig:
    if not env.get("HA_MCP_URL", "").strip():
        raise ConfigError("HA_MCP_URL is not set")
    return BridgeConfig(
        ha_mcp_url=parse_ha_mcp_url(env["HA_MCP_URL"]),
        endpoint_url=parse_endpoint_url(_read_endpoint(env)),
        voice_filter=load_voice_filter(env.get("VOICE_FILTER_FILE", DEFAULT_FILTER_FILE)),
        health_file=env.get("BRIDGE_HEALTH_FILE", DEFAULT_HEALTH_FILE),
        tunnel_iface=env.get("TUNNEL_IFACE", "").strip(),
        tools_cache=env.get("BRIDGE_TOOLS_CACHE", DEFAULT_TOOLS_CACHE).strip(),
        status_file=env.get("BRIDGE_STATUS_FILE", DEFAULT_STATUS_FILE).strip(),
    )
