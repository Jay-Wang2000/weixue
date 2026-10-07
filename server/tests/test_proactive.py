"""weixue_proactive: webhook auth, rate limit, dedup, queue rules, long-poll and delivery,
with fake sessions in place of ConnectionHandler and a fake speaker in place of TTS."""
import asyncio
import threading
import time
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import weixue_patches
import weixue_proactive as wp

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DEVICE = "7c:0c:5f:42:4b:fc"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Dialogue:
    def __init__(self):
        self.dialogue = []

    def put(self, message):
        self.dialogue.append(message)


class Conn:
    def __init__(self, device=DEVICE):
        self.device_id = device
        self.session_id = "session-1"
        self.tts = object()
        self.stop_event = threading.Event()
        self.client_is_speaking = False
        self.client_have_voice = False
        self.client_listen_mode = "auto"
        old = time.time() * 1000 - 10_000
        self.first_activity_time = old
        self.last_activity_time = old
        self.dialogue = Dialogue()
        self.closed = False

    def reset_audio_states(self):
        pass

    async def close(self):
        self.closed = True
        self.stop_event.set()


class Speaker:
    def __init__(self, outcome="spoken"):
        self.outcome = outcome
        self.calls = []

    async def __call__(self, conn, item, close_after):
        self.calls.append((item.id, close_after))
        conn.dialogue.put(types.SimpleNamespace(role="user", content="(event)"))
        conn._weixue_anchor = types.SimpleNamespace(role="assistant", content=item.text)
        conn.dialogue.put(conn._weixue_anchor)
        return self.outcome


def make(clock=None, speaker=None, token=TOKEN):
    return wp.Proactive(token, clock=clock or Clock(), wall=lambda: 1_790_000_000.0, speaker=speaker or Speaker())


def fields(key=None, **overrides):
    body = {"mode": "announce", "message": "Water detected under the sink."}
    body.update(overrides)
    return wp.parse_request(body, key)


# Request validation

@pytest.mark.parametrize(
    "body",
    [
        [],
        {"message": ""},
        {"message": "x" * (wp.MAX_MESSAGE_CHARS + 1)},
        {"message": "hi", "mode": "shout"},
        {"message": "hi", "ttl": 5},
        {"message": "hi", "ttl": wp.MAX_TTL_SECONDS + 1},
        {"message": "hi", "ttl": "60"},
        {"message": "hi", "ttl": True},
        {"message": "hi", "urgent": "yes"},
        {"message": "hi", "device": " "},
        {"message": "hi", "context": 3},
        {"message": "hi", "id": "k" * (wp.MAX_KEY_CHARS + 1)},
    ],
)
def test_invalid_requests_rejected(body):
    with pytest.raises(wp.RequestError) as err:
        wp.parse_request(body)
    assert err.value.status == 400


def test_request_defaults_and_header_key_wins():
    parsed = wp.parse_request({"message": " Hello ", "id": "body-key", "device": "7C:0C:5F:42:4B:FC"}, "header-key")
    assert parsed == {
        "mode": "announce",
        "text": "Hello",
        "context": "",
        "device": DEVICE,
        "urgent": False,
        "key": "header-key",
        "ttl": wp.DEFAULT_TTL_SECONDS,
    }


# Dedup and rate limit

def test_same_key_same_request_is_duplicate():
    p = make()
    status, first = p.submit(fields(key="leak-1"), "ha")
    status2, again = p.submit(fields(key="leak-1"), "ha")
    assert (status, status2) == (202, 200)
    assert again["id"] == first["id"] and again["duplicate"] is True
    assert len(p.queue) == 1


def test_same_key_different_request_conflicts():
    p = make()
    p.submit(fields(key="leak-1"), "ha")
    with pytest.raises(wp.RequestError) as err:
        p.submit(fields(key="leak-1", message="Something else."), "ha")
    assert err.value.status == 409


def test_keyless_repeat_is_duplicate_only_within_window():
    clock = Clock()
    p = make(clock)
    _, first = p.submit(fields(), "ha")
    _, again = p.submit(fields(), "ha")
    assert again["id"] == first["id"] and again["duplicate"]
    clock.t += wp.CONTENT_DEDUP_SECONDS + 1
    status, later = p.submit(fields(), "ha")
    assert status == 202 and later["id"] != first["id"]


def test_rate_limit_burst_then_refill():
    clock = Clock()
    p = make(clock)
    for i in range(wp.RATE_BURST):
        assert p.submit(fields(key=f"k{i}"), "ha")[0] == 202
    with pytest.raises(wp.RequestError) as err:
        p.submit(fields(key="over"), "ha")
    assert err.value.status == 429 and err.value.body["retry_after"] >= 1
    assert p.submit(fields(key="k0"), "ha")[0] == 200
    assert p.submit(fields(key="other-client"), "lan-host")[0] == 202
    clock.t += 1 / wp.RATE_PER_SECOND
    assert p.submit(fields(key="refilled"), "ha")[0] == 202


def test_queue_bound():
    clock = Clock()
    p = make(clock)
    for i in range(wp.MAX_QUEUED):
        p.submit(fields(key=f"k{i}"), f"client{i}")
    with pytest.raises(wp.RequestError) as err:
        p.submit(fields(key="one-more"), "fresh")
    assert err.value.status == 429 and err.value.body["error"] == "queue full"


# Delivery rules

def test_ttl_expiry():
    clock = Clock()
    p = make(clock)
    _, body = p.submit(fields(ttl=30), "ha")
    clock.t += 31
    assert p.next_for(DEVICE) is None
    assert p.messages[body["id"]].status == "expired"
    assert not p.queue


def test_reasons_offline_busy_muted():
    clock = Clock()
    p = make(clock)
    _, body = p.submit(fields(), "ha")
    item = p.messages[body["id"]]
    assert body["reason"] == "offline"
    p.last_poll[DEVICE] = clock.t
    assert p.reason(item) == ""
    clock.t += wp.POLL_SEEN_SECONDS + 1
    assert p.reason(item) == "offline"
    p.on_session_open(DEVICE, Conn())
    assert p.reason(item) == "busy"
    p.set_mute(10)
    assert p.reason(item) == "muted"


def test_mute_holds_all_but_urgent():
    clock = Clock()
    p = make(clock)
    p.set_mute(5)
    p.submit(fields(key="normal"), "ha")
    assert p.next_for(DEVICE) is None
    p.submit(fields(key="urgent", urgent=True), "ha")
    assert p.next_for(DEVICE).urgent
    clock.t += 5 * 60
    assert p.mute_state()["muted"] is False
    assert p.next_for(DEVICE).key == "normal"


def test_device_targeting():
    p = make()
    p.submit(fields(key="other", device="aa:aa:aa:aa:aa:aa"), "ha")
    assert p.next_for(DEVICE) is None
    p.submit(fields(key="any"), "ha")
    assert p.next_for(DEVICE).key == "any"


# Long-poll

async def test_poll_returns_when_message_arrives(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 5)
    p = make(clock=time.monotonic)
    waiter = asyncio.create_task(p.poll(DEVICE))
    await asyncio.sleep(0.1)
    assert not waiter.done()
    p.submit(fields(), "ha")
    assert await asyncio.wait_for(waiter, 1) is True


async def test_poll_times_out_without_message(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    p = make(clock=time.monotonic)
    assert await p.poll(DEVICE) is False
    assert DEVICE in p.last_poll


async def test_poll_ignores_session_older_than_poll(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    clock = Clock()
    p = make(clock)
    p.on_session_open(DEVICE, Conn())
    p.submit(fields(), "ha")
    clock.t += 1
    assert await p.poll(DEVICE) is True
    assert p.live_sessions(DEVICE) == []


async def test_poll_holds_while_live_session_open(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    p = make(clock=time.monotonic)
    waiter = asyncio.create_task(p.poll(DEVICE))
    await asyncio.sleep(0.05)
    p.on_session_open(DEVICE, Conn())
    p.submit(fields(), "ha")
    assert await waiter is False


# Delivery

async def test_invoked_announce_is_delivered_and_closes_session():
    speaker = Speaker()
    p = make(speaker=speaker)
    _, body = p.submit(fields(), "ha")
    conn = Conn()
    p.on_session_open(DEVICE, conn)
    await p.on_invoke(conn)
    await p._delivery
    assert speaker.calls == [(body["id"], True)]
    assert p.messages[body["id"]].status == "delivered"
    assert not p.queue and conn._weixue_busy is False


async def test_invoke_with_nothing_pending_closes():
    p = make()
    conn = Conn()
    await p.on_invoke(conn)
    assert conn.closed and p._delivery is None


async def test_invoke_does_not_block_the_session_loop():
    started = asyncio.Event()

    async def slow(conn, item, close_after):
        started.set()
        await asyncio.sleep(10)
        return "spoken"

    p = make(speaker=slow)
    p.submit(fields(), "ha")
    await asyncio.wait_for(p.on_invoke(Conn()), 0.1)
    await asyncio.wait_for(started.wait(), 1)
    p._delivery.cancel()


async def test_failed_delivery_requeues_then_fails():
    speaker = Speaker(outcome="closed")
    p = make(speaker=speaker)
    _, body = p.submit(fields(), "ha")
    item = p.messages[body["id"]]
    for attempt in range(1, wp.MAX_ATTEMPTS):
        await p.deliver(Conn(), item)
        assert (item.status, item.attempts) == ("queued", attempt)
    await p.deliver(Conn(), item)
    assert item.status == "failed" and item.error == "closed"
    assert not p.queue


async def test_conversation_reply_is_recorded():
    p = make()
    p.reply_window = 1
    _, body = p.submit(fields(mode="conversation", message="Shall I close the valve?"), "ha")
    conn = Conn()
    p.on_session_open(DEVICE, conn)

    async def answer():
        await asyncio.sleep(0.2)
        conn.dialogue.put(types.SimpleNamespace(role="user", content="给我讲个故事吧", is_temporary=True))
        conn.dialogue.put(types.SimpleNamespace(role="user", content='{"speaker": "", "content": "Yes."}'))

    asyncio.create_task(answer())
    await p.on_invoke(conn)
    await p._delivery
    item = p.messages[body["id"]]
    assert item.status == "answered" and item.reply == "Yes."
    assert not conn.closed


async def test_conversation_without_reply_closes_invoked_session():
    speaker = Speaker()
    p = make(speaker=speaker)
    p.reply_window = 0.3
    _, body = p.submit(fields(mode="conversation", message="Shall I close the valve?"), "ha")
    conn = Conn()
    await p.on_invoke(conn)
    await p._delivery
    assert speaker.calls == [(body["id"], False)]
    assert p.messages[body["id"]].status == "no_reply"
    assert conn.closed


async def test_reply_window_extends_while_user_is_speaking():
    p = make()
    p.reply_window = 0.2
    _, body = p.submit(fields(mode="conversation"), "ha")
    conn = Conn()
    conn.client_have_voice = True

    async def answer():
        await asyncio.sleep(0.5)
        conn.client_have_voice = False
        conn.dialogue.put(types.SimpleNamespace(role="user", content="嗯"))

    asyncio.create_task(answer())
    await p.on_invoke(conn)
    await p._delivery
    assert p.messages[body["id"]].reply == "嗯"


async def test_dispatch_waits_for_quiet_session():
    speaker = Speaker()
    p = make(speaker=speaker)
    conn = Conn()
    p.on_session_open(DEVICE, conn)
    _, body = p.submit(fields(), "ha")
    conn.client_is_speaking = True
    p.dispatch_once()
    await asyncio.sleep(0)
    assert speaker.calls == []
    conn.client_is_speaking = False
    conn.last_activity_time = time.time() * 1000
    p.dispatch_once()
    assert speaker.calls == []
    conn.last_activity_time -= wp.SESSION_QUIET_SECONDS * 1000
    p.dispatch_once()
    await asyncio.sleep(0.05)
    assert speaker.calls == [(body["id"], False)]
    assert p.messages[body["id"]].status == "delivered"
    assert not conn.closed


async def test_dispatch_skips_new_and_stale_sessions():
    speaker = Speaker()
    clock = Clock()
    p = make(clock, speaker)
    fresh = Conn()
    fresh.first_activity_time = time.time() * 1000
    p.on_session_open(DEVICE, fresh)
    p.submit(fields(), "ha")
    p.dispatch_once()
    await asyncio.sleep(0)
    assert speaker.calls == []
    p.on_session_close(DEVICE, fresh)
    stale = Conn()
    p.on_session_open(DEVICE, stale)
    clock.t += 1
    p.poll_started[DEVICE] = clock.t
    p.dispatch_once()
    await asyncio.sleep(0)
    assert speaker.calls == []


# Upstream hooks

async def test_register_sessions_tracks_open_session():
    p = make()
    seen = []

    async def handle_connection(self, ws):
        seen.append(list(p.sessions))

    wrapped = weixue_patches.register_sessions(handle_connection, p)
    ws = types.SimpleNamespace(request=types.SimpleNamespace(headers={"device-id": "7C:0C:5F:42:4B:FC"}))
    await wrapped(Conn(), ws)
    assert seen == [[DEVICE]] and p.sessions == {}


async def test_intercept_invoke_only_for_invoke_text():
    calls = []

    class FakeProactive:
        async def on_invoke(self, conn):
            calls.append("invoke")

    async def handle(self, conn, msg_json):
        calls.append(msg_json["text"])

    wrapped = weixue_patches.intercept_invoke(handle, FakeProactive(), wp.INVOKE_TEXT)
    conn = Conn()
    await wrapped(None, conn, {"type": "listen", "state": "detect", "text": wp.INVOKE_TEXT, "mode": "realtime"})
    await wrapped(None, conn, {"type": "listen", "state": "detect", "text": "Jarvis"})
    assert calls == ["invoke", "Jarvis"]
    assert conn.client_listen_mode == "realtime"


def test_web_with_routes_adds_routes():
    shim = weixue_patches.WebWithRoutes(web, lambda app: wp.add_routes(app, make()))
    app = shim.Application()
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/weixue/proactive/messages" in paths and "/weixue/proactive/poll" in paths
    assert shim.get is web.get


# HTTP

@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    p = make(clock=time.monotonic)
    app = web.Application()
    wp.add_routes(app, p)
    async with TestClient(TestServer(app)) as c:
        c.proactive = p
        yield c
    p._dispatcher.cancel()


async def test_http_auth(client):
    body = {"message": "hi"}
    assert (await client.post("/weixue/proactive/messages", json=body)).status == 401
    wrong = {"Authorization": "Bearer nope"}
    assert (await client.post("/weixue/proactive/messages", json=body, headers=wrong)).status == 401
    assert (await client.get("/weixue/proactive", headers=wrong)).status == 401
    assert (await client.post("/weixue/proactive/messages", json=body, headers=AUTH)).status == 202


async def test_http_disabled_without_token(client):
    client.proactive.token = ""
    resp = await client.post("/weixue/proactive/messages", json={"message": "hi"}, headers=AUTH)
    assert resp.status == 503


async def test_http_create_status_dedup_and_rate_limit(client):
    headers = {**AUTH, "Idempotency-Key": "leak-1"}
    resp = await client.post("/weixue/proactive/messages", json={"message": "hi", "mode": "conversation"}, headers=headers)
    created = await resp.json()
    assert resp.status == 202 and created["status"] == "queued" and created["reason"] == "offline"
    again = await client.post("/weixue/proactive/messages", json={"message": "hi", "mode": "conversation"}, headers=headers)
    assert again.status == 200 and (await again.json())["duplicate"] is True
    got = await client.get(f"/weixue/proactive/messages/{created['id']}", headers=AUTH)
    assert got.status == 200 and (await got.json())["id"] == created["id"]
    assert (await client.get("/weixue/proactive/messages/missing", headers=AUTH)).status == 404
    for i in range(wp.RATE_BURST - 1):
        await client.post("/weixue/proactive/messages", json={"message": f"m{i}"}, headers=AUTH)
    limited = await client.post("/weixue/proactive/messages", json={"message": "late"}, headers=AUTH)
    assert limited.status == 429 and int(limited.headers["Retry-After"]) >= 1
    bad = await client.post("/weixue/proactive/messages", data="not json", headers=AUTH)
    assert bad.status == 400


async def test_http_mute_and_status(client):
    assert (await client.put("/weixue/proactive/mute", json={"minutes": -1}, headers=AUTH)).status == 400
    resp = await client.put("/weixue/proactive/mute", json={"minutes": 30}, headers=AUTH)
    assert (await resp.json())["muted"] is True
    status = await (await client.get("/weixue/proactive", headers=AUTH)).json()
    assert status["muted"] is True and status["queued"] == []
    resp = await client.put("/weixue/proactive/mute", json={"minutes": 0}, headers=AUTH)
    assert (await resp.json())["muted"] is False


async def test_http_poll(client):
    assert (await client.get("/weixue/proactive/poll")).status == 400
    assert (await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE})).status == 204
    await client.post("/weixue/proactive/messages", json={"message": "hi"}, headers=AUTH)
    resp = await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE.upper()})
    assert resp.status == 200 and (await resp.json()) == {"pending": True, "urgent": False}


async def test_http_poll_reports_urgent(client):
    await client.post("/weixue/proactive/messages", json={"message": "leak", "urgent": True}, headers=AUTH)
    resp = await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE})
    assert resp.status == 200 and (await resp.json()) == {"pending": True, "urgent": True}


async def test_speak_queues_one_assistant_turn(monkeypatch):
    import queue

    import core.handle.sendAudioHandle as send_audio
    from core.providers.tts.dto.dto import ContentType, SentenceType

    sent = []

    async def send_tts_message(conn, state, text=None):
        sent.append(state)

    class TTS:
        def __init__(self):
            self.tts_text_queue = queue.Queue()
            self.stored = {}

        def store_tts_text(self, sentence_id, text):
            self.stored[sentence_id] = text

        def tts_one_sentence(self, conn, content_type, content_detail=None, sentence_id=None):
            self.tts_text_queue.put(types.SimpleNamespace(sentence_type=SentenceType.MIDDLE, content_type=content_type, content_detail=content_detail, sentence_id=sentence_id))

    monkeypatch.setattr(send_audio, "send_tts_message", send_tts_message)
    p = make()
    _, body = p.submit(fields(mode="conversation", message="Shall I close the valve?", context="Leak sensor kitchen."), "ha")
    conn = Conn()
    conn.tts = TTS()

    async def finish():
        await asyncio.sleep(0.2)
        conn.client_is_speaking = False

    asyncio.create_task(finish())
    outcome = await wp.speak(conn, p.messages[body["id"]], close_after=False)
    assert outcome == "spoken" and sent == ["start"]
    items = []
    while not conn.tts.tts_text_queue.empty():
        items.append(conn.tts.tts_text_queue.get())
    assert [i.sentence_type for i in items] == [SentenceType.FIRST, SentenceType.MIDDLE, SentenceType.LAST]
    assert items[1].content_type == ContentType.TEXT and items[1].content_detail == "Shall I close the valve?"
    assert len({i.sentence_id for i in items} | {conn.sentence_id}) == 1
    roles = [(m.role, m.content) for m in conn.dialogue.dialogue]
    assert roles[0][0] == "user" and "Leak sensor kitchen." in roles[0][1] and "Reply in English" in roles[0][1]
    assert roles[1] == ("assistant", "Shall I close the valve?")
    assert conn.close_after_chat is False


async def test_speak_reports_closed_session():
    conn = Conn()
    conn.tts = None
    conn.stop_event.set()
    p = make()
    _, body = p.submit(fields(), "ha")
    assert await wp.speak(conn, p.messages[body["id"]], close_after=True) == "closed"
