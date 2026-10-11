import asyncio
import json
import sys
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_hamcp import FakeHaMcp  # noqa: E402
from fake_api import FakeApi  # noqa: E402
from weixue_agent import ha_adapter, interactions, turns  # noqa: E402
from weixue_agent.api import Api  # noqa: E402
from weixue_agent.config import load_settings  # noqa: E402
from weixue_agent.service import Service  # noqa: E402
from weixue_hamcp import HaMcpClient  # noqa: E402

TOKEN = "t" * 40
HA_URL = "http://ha.test:9584/private_test"
DEVICE = "sim-device-01"


async def idle_script(turn):
    await turn.text("Hello there.")


def parse_sse(text: str):
    events = []
    for block in text.split("\n\n"):
        data = [line[5:].strip() for line in block.split("\n") if line.startswith("data:")]
        if data:
            events.append(json.loads("\n".join(data)))
    return events


class DroppingTransport(httpx.AsyncBaseTransport):
    """Forwards to the fake ha-mcp; for paths in `drop`, the response is lost after the request was handled; for
    paths in `refuse`, the connection fails before anything is sent."""

    def __init__(self, inner):
        self.inner = inner
        self.drop = set()
        self.refuse = set()

    async def handle_async_request(self, request):
        if any(request.url.path.endswith(p) for p in self.refuse):
            raise httpx.ConnectError("connection refused", request=request)
        response = await self.inner.handle_async_request(request)
        if any(request.url.path.endswith(p) for p in self.drop):
            raise httpx.ReadError("connection dropped", request=request)
        return response


class Harness:
    def __init__(self, tmp_path: Path, script, fake_ha: FakeHaMcp):
        env = {
            "AGENT_SERVICE_TOKEN": TOKEN, "HA_MCP_URL": HA_URL, "AGENT_DATA_DIR": str(tmp_path / "data"),
            "ANTHROPIC_API_KEY": "sk-ant-test-key-0000000000", "TZ": "Europe/Dublin", "AGENT_MCP_ENDPOINT_HOST": "",
        }
        self.tmp = tmp_path
        self.env = env
        self.settings = load_settings(env)
        self.api = FakeApi(script)
        self.fake_ha = fake_ha
        self.service = self._service()
        self.http = self._http()

    def _service(self):
        transport = self.transport = DroppingTransport(httpx.ASGITransport(app=self.fake_ha.app()))
        service = Service(self.settings, client=self.api.client(),
                          ha_client=HaMcpClient(HA_URL, transport=transport))
        service.engine.prefix_mismatch_behavior = "error"
        return service

    def _http(self):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=Api(self.service).app()), base_url="http://agent",
                                 headers={"Authorization": f"Bearer {TOKEN}"}, timeout=60)

    @property
    def engine(self):
        return self.service.engine

    @property
    def store(self):
        return self.service.store

    async def start(self):
        await self.service.ha.refresh()
        await self.service.start(background=False)

    async def restart(self):
        """Simulated process restart: same data directory, new service object."""
        await self.http.aclose()
        await self.service.engine.shutdown()
        self.service.store.close()
        self.service = self._service()
        self.http = self._http()
        await self.start()

    @property
    def factory(self):
        return self.api

    async def stop(self):
        await self.http.aclose()
        await self.service.stop()
        self.api.close()

    async def poll(self):
        await self.service.ha.poll_once()

    async def send(self, text, chat="current", key=None, **extra):
        body = {"device_id": DEVICE, "text": text, **extra}
        resp = await self.http.post(f"/v1/chats/{chat}/messages", json=body,
                                    headers={"Idempotency-Key": key or str(uuid.uuid4()),
                                             "Accept": "text/event-stream"})
        assert resp.status_code == 200, resp.text
        return resp, parse_sse(resp.text)

    async def send_json(self, text, chat="current", key=None, expect=(200, 201), **extra):
        body = {"device_id": DEVICE, "text": text, **extra}
        resp = await self.http.post(f"/v1/chats/{chat}/messages", json=body,
                                    headers={"Idempotency-Key": key or str(uuid.uuid4())})
        assert resp.status_code in expect, resp.text
        return resp.json()

    async def wait_task(self, task_id, states, timeout=10.0):
        states = (states,) if isinstance(states, str) else states
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            task = self.store.task(task_id)
            if task["state"] in states and not (task["state"].startswith("needs_") and
                                                self.engine.turn_in_flight(task["chat_id"])):
                return task
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"task {task_id} is {task['state']}, wanted {states}")
            await asyncio.sleep(0.02)

    async def wait_for(self, predicate, timeout=10.0, what="condition"):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            value = predicate()
            if value:
                return value
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"timed out waiting for {what}")
            await asyncio.sleep(0.02)

    async def idle(self, chat_id, timeout=10.0):
        await self.wait_for(lambda: not self.engine.turn_in_flight(chat_id) and not self.engine.rt(chat_id).queue,
                            timeout, "chat idle")

    def transcript(self, chat_id):
        from weixue_agent.transcript import Transcript
        return Transcript(self.store, chat_id)

    def events(self, chat_id, type_=None):
        events = self.service.hub.replay(chat_id, 0)
        return [e for e in events if type_ is None or e["type"] == type_]

    def events_of_type(self, type_):
        rows = self.store.all("SELECT * FROM chat_events WHERE type = ? ORDER BY ts, seq", (type_,))
        from weixue_agent.sse import envelope
        return [envelope(r) for r in rows]

    def types(self, chat_id):
        return [e["type"] for e in self.service.hub.replay(chat_id, 0)]

    def deliveries(self, **where):
        rows = self.store.all("SELECT * FROM deliveries ORDER BY gseq")
        return [r for r in rows if all(r.get(k) == v for k, v in where.items())]


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch):
    monkeypatch.setattr(ha_adapter, "HA_CALL_BUDGET_S", 0.3)
    monkeypatch.setattr(turns, "QUEUE_WAIT_MS", 300)
    monkeypatch.setattr(turns, "TEXT_COALESCE_MS", 10)
    monkeypatch.setattr(turns, "ACK_AFTER_S", 1.0)
    monkeypatch.setattr(turns, "REPLY_HOLD_S", 0.4)
    monkeypatch.setattr(turns, "STEER_WAIT_S", 0.3)
    monkeypatch.setattr(turns, "CANCEL_MAX_S", 1.0)
    monkeypatch.setattr(turns, "BARGE_IN_RESUME_S", 0.5)
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 1.0)


@pytest_asyncio.fixture
async def make(tmp_path):
    harnesses = []

    async def _make(script=idle_script, fake_ha=None):
        h = Harness(tmp_path / f"h{len(harnesses)}", script, fake_ha or FakeHaMcp(wait_seconds=2.0))
        await h.start()
        harnesses.append(h)
        return h

    yield _make
    for h in harnesses:
        await h.stop()
