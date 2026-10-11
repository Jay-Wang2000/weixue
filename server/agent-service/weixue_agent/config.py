"""Service configuration and constants. Error messages never echo a value."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from weixue_hamcp import UrlError, parse_ha_mcp_url

REPLY_HOLD_S = 6
ACK_AFTER_S = 20
STEER_WAIT_S = 3
CANCEL_MAX_S = 2
TURN_MAX_S = 1800
HEARTBEAT_REPLY_S = 2
HEARTBEAT_S = 5
QUEUE_WAIT_MS = 1500
STREAMS_MAX = 8
ACTOR_IDLE_S = 300
QUICK_MAX_ROUNDS = 3
MAX_ITERATIONS = 60
TURN_BUDGET_USD = 25.0
PAUSE_RESUME_MAX = 5
SETTLE_MAX = 3
COMPACT_AT_TOKENS = 150_000
VOICE_WINDOW_S = 20
INPUT_TTL_S = 1800
BARGE_IN_RESUME_S = 10
MAX_OPEN_TASKS_PER_DEVICE = 20
HA_CALL_BUDGET_S = 8
HA_PENDING_POLL_S = 2
HA_IDLE_POLL_S = 60
HA_TOOLS_REFRESH_S = 900
HA_TOOLS_RETRY_MIN_S = 5
HA_TOOLS_RETRY_MAX_S = 300
HA_UNKNOWN_FALLBACK_TTL_S = 3600
HA_MCP_FALLBACK_WAIT_S = 180
HA_PING_MAX_AGE_S = 60
EVENT_RETENTION_DAYS = 30
IDEMPOTENCY_TTL_S = 24 * 3600
DONE_SPEECH_TTL_S = 24 * 3600
NOTIFY_TTL_S = 3600
HEALTH_PROBE_S = 10
HEALTH_PROBE_TIMEOUT_S = 5
# A health_probes row is written when a probe's ok flips, else at most this often.
PROBE_ROW_EVERY_S = 300
DELIVERY_LEASE_S = 30
TEXT_COALESCE_MS = 150
DEVICE_ACTION_WAIT_S = 8
PROGRESS_MIN_INTERVAL_S = 2
NAMESPACE_CHECK_S = 2
MEMORY_FILE_MAX_BYTES = 64 * 1024

TITLE_MAX = 32
SUMMARY_MAX = 60
OPTION_MAX = 24
QUESTION_MAX = 120
ACTION_MAX = 60
DEVICE_RESULT_MAX = 2000

# Environment kept for the service; everything else is dropped at start.
ENV_KEEP = frozenset({
    "PATH", "HOME", "HOSTNAME", "LANG", "LANGUAGE", "TZ", "TERM", "USER", "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "SSL_CERT_FILE", "SSL_CERT_DIR", "ANTHROPIC_API_KEY",
})
ENV_KEEP_PREFIXES = ("LC_",)


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    token: str
    ha_mcp_url: Optional[str]
    db_path: Path
    data_dir: Path
    memory_dir: Path
    host: str
    port: int
    tz: str
    anthropic_key_present: bool
    mcp_endpoint_host: Optional[str]
    monitor_token: Optional[str] = None
    probe_s: float = HEALTH_PROBE_S
    probe_timeout_s: float = HEALTH_PROBE_TIMEOUT_S

    @property
    def e2e_guard(self) -> bool:
        """The end-to-end test guard is on while `<data dir>/e2e-guard` exists."""
        return (self.data_dir / "e2e-guard").exists()


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    token = env.get("AGENT_SERVICE_TOKEN", "").strip()
    if len(token) < 32:
        raise ConfigError("AGENT_SERVICE_TOKEN is unset or shorter than 32 characters")
    raw_url = env.get("HA_MCP_URL", "").strip()
    try:
        ha_url = parse_ha_mcp_url(raw_url) if raw_url else None
    except UrlError as e:
        raise ConfigError(str(e)) from None
    try:
        port = int(env.get("AGENT_PORT", "8010"))
    except ValueError:
        raise ConfigError("AGENT_PORT must be an integer") from None
    monitor_token = env.get("MONITOR_TOKEN", "").strip() or None
    if monitor_token is not None and (len(monitor_token) < 32 or monitor_token == token):
        raise ConfigError("MONITOR_TOKEN must be at least 32 characters and differ from AGENT_SERVICE_TOKEN")
    try:
        probe_s = float(env.get("AGENT_HA_PROBE_S", HEALTH_PROBE_S))
        probe_timeout_s = float(env.get("AGENT_HA_PROBE_TIMEOUT_S", HEALTH_PROBE_TIMEOUT_S))
    except ValueError:
        raise ConfigError("AGENT_HA_PROBE_S and AGENT_HA_PROBE_TIMEOUT_S must be numbers") from None
    if not 0 < probe_timeout_s < probe_s:
        raise ConfigError("AGENT_HA_PROBE_TIMEOUT_S must be positive and below AGENT_HA_PROBE_S")
    data_dir = Path(env.get("AGENT_DATA_DIR", "/data"))
    return Settings(
        token=token,
        ha_mcp_url=ha_url,
        db_path=Path(env.get("AGENT_DB", str(data_dir / "db" / "agent.db"))),
        data_dir=data_dir,
        memory_dir=Path(env.get("AGENT_MEMORY_DIR", str(data_dir / "memory"))),
        host=env.get("AGENT_HOST", "0.0.0.0"),
        port=port,
        tz=env.get("TZ", "Europe/Dublin") or "Europe/Dublin",
        anthropic_key_present=bool(env.get("ANTHROPIC_API_KEY", "").strip()),
        mcp_endpoint_host=env.get("AGENT_MCP_ENDPOINT_HOST", "mcp-endpoint-server:8004") or None,
        monitor_token=monitor_token,
        probe_s=probe_s,
        probe_timeout_s=probe_timeout_s,
    )


def scrub_environment(environ=os.environ) -> list:
    """Drop every variable the service does not need; returns the dropped names."""
    dropped = [k for k in list(environ) if k not in ENV_KEEP and not k.startswith(ENV_KEEP_PREFIXES)]
    for key in dropped:
        del environ[key]
    return sorted(dropped)
