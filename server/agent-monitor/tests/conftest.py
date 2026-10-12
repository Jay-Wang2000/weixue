import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "agent-service"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fixture_db  # noqa: E402
from agent_monitor.app import create_app  # noqa: E402
from agent_monitor.config import Settings  # noqa: E402
from agent_monitor.db import ReadOnlyDb  # noqa: E402
from agent_monitor.live import LiveSources  # noqa: E402

MONITOR_TOKEN = "m" * 40


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "db" / "agent.db"
    store = fixture_db.build(path)
    yield path
    store.close()


@pytest.fixture
def db(db_path):
    return ReadOnlyDb(str(db_path))


class FakeUpstream:
    """agent-service /healthz + /v1/status and the gateway summary, recording the Authorization headers."""

    def __init__(self):
        self.health = {"ok": True, "db": True, "model": True, "ha_mcp": True}
        self.status_code = 200
        self.down = set()
        self.auth = []
        self.devices = {fixture_db.DEVICE: {"session_open": False, "poll_held": True, "last_poll_seconds_ago": 0.4,
                                            "last_poll_end_seconds_ago": 25.1, "reachable": True}}
        self.probe_ts = fixture_db.NOW.timestamp() - 4

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.append((request.url.path, request.headers.get("authorization")))
        host = request.url.host
        if host in self.down:
            raise httpx.ConnectError("down", request=request)
        if request.url.path == "/healthz":
            return httpx.Response(200 if self.health.get("ok") else 503, json=self.health)
        if request.url.path == "/v1/status":
            return httpx.Response(self.status_code, json={
                "open_tasks": 1, "turns_in_flight": 0, "open_holds": 1, "journal_mode": "wal",
                "actors": [{"chat_id": fixture_db.CHAT_B, "busy": True, "queued": 1, "turn_id": "x", "mode": "task",
                            "idle_s": 1.0}],
                "ha_mcp": {"configured": True, "tool_count": 77, "policy_version": 3, "wait_seconds": 8,
                           "last_error": "boom at http://ha.local:9584/private_abc", "pending_count": 3},
                "probes": {"ha_mcp": {"ok": True, "latency_ms": 180, "ts": self.probe_ts, "tool_count": 77,
                                      "policy_version": 3, "wait_seconds": 8, "pending_count": 3,
                                      "pending_not_ours": 2},
                           "tunnel": {"ok": True, "latency_ms": None, "ts": self.probe_ts, "rx_bytes": 1}},
                "recovery": {"secret": "RECOVERY_SECRET"},
            })
        if request.url.path == "/weixue/proactive":
            return httpx.Response(200, json={"devices": self.devices, "muted": False,
                                             "queued": [{"id": "q", "text": "QUEUED_SECRET"}]})
        return httpx.Response(404)


@pytest.fixture
def upstream():
    return FakeUpstream()


@pytest.fixture
def make_app(db_path, tmp_path, upstream):
    def _make(clock=lambda: fixture_db.NOW.timestamp(), **overrides):
        bridge = tmp_path / "bridge.json"
        if not bridge.exists():
            bridge.write_text(json.dumps({"ts": clock() - 10, "state": "connected", "announced": True,
                                          "ping_first_byte_ms_p50": 90, "tools": 77, "tools_source": "live"}))
        settings = Settings(db_path=str(db_path), bridge_status=str(bridge), agent_url="http://agent-service:8010",
                            gateway_url="http://xiaozhi-esp32-server:8003", mcp_endpoint="",
                            **{"monitor_token": MONITOR_TOKEN, **overrides})
        client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        live = LiveSources(settings, client=client, clock=clock)
        return create_app(settings, live=live, clock=clock, background=False)
    return _make


@pytest.fixture
async def api(make_app):
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://monitor",
                                 headers={"Authorization": f"Bearer {MONITOR_TOKEN}"}) as client:
        client.app = app
        await app.state.board.live.refresh()
        yield client
