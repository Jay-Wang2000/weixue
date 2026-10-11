"""weixue_proactive: webhook auth, rate limit, dedup, queue rules, device auth, long-poll
reservations and delivery claims, with fake sessions in place of ConnectionHandler and a fake
speaker in place of TTS."""
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
OTHER = "aa:aa:aa:aa:aa:aa"


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
    def __init__(self, device=DEVICE, session_id="session-1"):
        self.device_id = device
        self.headers = {"device-id": device}
        self.session_id = session_id
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


@pytest.fixture(autouse=True)
def quick_drain(monkeypatch):
    monkeypatch.setattr(wp, "DRAIN_QUIET_SECONDS", 0)


async def drained(conn):
    """Whether the rejected session's drain task closed conn."""
    for _ in range(5):
        await asyncio.sleep(0)
    return conn.closed


def make(clock=None, speaker=None, token=TOKEN, auth=None):
    return wp.Proactive(
        token,
        clock=clock or Clock(),
        wall=lambda: 1_790_000_000.0,
        speaker=speaker or Speaker(),
        auth=auth or wp.DeviceAuth(True, [DEVICE]),
    )


class Ws:
    """websockets.ServerConnection.ping(): answers unless dead."""

    def __init__(self, dead=False):
        self.dead = dead
        self.pings = 0

    async def ping(self):
        self.pings += 1
        waiter = asyncio.get_running_loop().create_future()
        if not self.dead:
            waiter.set_result(0.001)
        return waiter


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
    _, body = p.submit(fields(), "ha")
    item = await asyncio.wait_for(waiter, 1)
    assert item.id == body["id"] and item.reserved_for == DEVICE


async def test_poll_times_out_without_message(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    p = make(clock=time.monotonic)
    assert await p.poll(DEVICE) is None
    assert DEVICE in p.last_poll and p.polls == {}


async def test_poll_drops_a_session_that_does_not_answer_a_ping(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    monkeypatch.setattr(wp, "PROBE_TIMEOUT_SECONDS", 0.05)
    p = make(clock=time.monotonic)
    stale = Conn()
    p.on_session_open(DEVICE, stale, Ws(dead=True))
    p.submit(fields(), "ha")
    assert await p.poll(DEVICE) is not None
    assert p.live_sessions(DEVICE) == []
    await asyncio.sleep(0)
    assert stale.closed


async def test_poll_from_a_second_client_does_not_evict_a_live_session(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    p = make(clock=time.monotonic)
    live, ws = Conn(), Ws()
    p.on_session_open(DEVICE, live, ws)
    p.submit(fields(), "ha")
    for _ in range(3):
        assert await p.poll(DEVICE) is None
    assert ws.pings == 3 and p.live_sessions(DEVICE) == [live] and not live.closed
    assert p.reason(p.queue[0]) == "busy"


async def test_poll_holds_while_live_session_open(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    p = make(clock=time.monotonic)
    waiter = asyncio.create_task(p.poll(DEVICE))
    await asyncio.sleep(0.05)
    p.on_session_open(DEVICE, Conn())
    p.submit(fields(), "ha")
    assert await waiter is None


async def test_newer_poll_from_the_device_ends_the_older_one(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 5)
    p = make(clock=time.monotonic)
    older = asyncio.create_task(p.poll(DEVICE))
    await asyncio.sleep(0.05)
    newer = asyncio.create_task(p.poll(DEVICE))
    assert await asyncio.wait_for(older, 1) is wp.REPLACED
    assert not newer.done()
    p.submit(fields(), "ha")
    assert await asyncio.wait_for(newer, 1) is not None


async def test_held_polls_and_known_devices_are_bounded(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 5)
    devices = [f"02:00:00:00:00:{i:02x}" for i in range(wp.MAX_DEVICES + 4)]
    p = make(clock=time.monotonic, auth=wp.DeviceAuth(True, devices))
    held = [asyncio.create_task(p.poll(d)) for d in devices[: wp.MAX_HELD_POLLS]]
    await asyncio.sleep(0.05)
    with pytest.raises(wp.RequestError) as err:
        await p.poll(devices[wp.MAX_HELD_POLLS])
    assert err.value.status == 429
    for task in held:
        task.cancel()
    await asyncio.gather(*held, return_exceptions=True)
    for d in devices:
        p._seen(d)
    assert len(p.last_poll) == wp.MAX_DEVICES and devices[-1] in p.last_poll


# Reservations (the poll's answer names the message the invoked session delivers)

def test_reservation_survives_ttl_until_it_lapses():
    clock = Clock()
    p = make(clock)
    _, a = p.submit(fields(key="a", ttl=wp.MIN_TTL_SECONDS), "ha")
    item = p.reserve(DEVICE)
    assert item.id == a["id"]
    clock.t += wp.MIN_TTL_SECONDS + 1
    assert p.leased(DEVICE, clock.t) is item and item.status == "queued"
    clock.t += wp.RESERVE_SECONDS
    assert p.leased(DEVICE, clock.t) is None and item.status == "expired"


def test_expired_lease_is_not_consumable_while_the_message_lives():
    clock = Clock()
    p = make(clock)
    p.submit(fields(ttl=wp.MAX_TTL_SECONDS), "ha")
    item = p.reserve(DEVICE)
    lease = item.lease
    clock.t += wp.RESERVE_SECONDS
    assert item.status == "queued" and item.reserved_for == DEVICE
    assert p.leased(DEVICE, clock.t) is None
    assert p.leased(DEVICE, clock.t, item.id, lease) is None


def test_a_new_lease_releases_the_device_s_older_one():
    clock = Clock()
    p = make(clock)
    p.set_mute(10)
    _, a = p.submit(fields(key="a", urgent=True), "ha")
    first = p.reserve(DEVICE)
    old_lease = first.lease
    _, b = p.submit(fields(key="b"), "ha")
    p.set_mute(0)
    clock.t += 1
    again = p.reserve(DEVICE)
    assert again is first and again.lease != old_lease
    assert p.leased(DEVICE, clock.t, first.id, old_lease) is None
    assert p.messages[b["id"]].reserved_for is None


async def test_invoke_naming_its_lease_delivers_only_that_message():
    clock = Clock()
    speaker = Speaker()
    p = make(clock, speaker)
    _, a = p.submit(fields(key="a"), "ha")
    item = p.reserve(DEVICE)
    conn = Conn()
    p.on_session_open(DEVICE, conn)
    await p.on_invoke(conn, f"{wp.INVOKE_TEXT}:{item.id}:{item.lease}")
    await p._delivery
    assert speaker.calls == [(a["id"], True)] and p.messages[a["id"]].status == "delivered"


@pytest.mark.parametrize("binding", ["{id}:wrong", "other:{lease}", "{id}", "{id}:{lease}:x", ":"])
async def test_invoke_with_a_wrong_binding_consumes_nothing(binding):
    clock = Clock()
    p = make(clock)
    _, a = p.submit(fields(key="a"), "ha")
    item = p.reserve(DEVICE)
    conn = Conn()
    p.on_session_open(DEVICE, conn)
    await p.on_invoke(conn, f"{wp.INVOKE_TEXT}:" + binding.format(id=item.id, lease=item.lease))
    assert await drained(conn) and p._delivery is None
    assert (item.status, item.attempts, item.reserved_for) == ("queued", 0, DEVICE)


async def test_delayed_invoke_does_not_consume_a_newer_lease():
    clock = Clock()
    speaker = Speaker()
    p = make(clock, speaker)
    _, a = p.submit(fields(key="a", ttl=wp.MAX_TTL_SECONDS), "ha")
    first = p.reserve(DEVICE)
    a_lease = first.lease
    stale = Conn(session_id="stale")
    p.on_session_open(DEVICE, stale)
    clock.t += wp.RESERVE_SECONDS + 1
    p.on_session_close(DEVICE, stale)
    _, b = p.submit(fields(key="b", urgent=True, message="Leak!"), "ha")
    p.queue.rotate(1)
    newer = p.reserve(DEVICE)
    assert newer.id == b["id"]
    await p.on_invoke(stale)
    assert await drained(stale) and p._delivery is None
    bound = Conn(session_id="bound")
    p.on_session_open(DEVICE, bound)
    await p.on_invoke(bound, f"{wp.INVOKE_TEXT}:{first.id}:{a_lease}")
    assert await drained(bound) and p._delivery is None
    assert (newer.status, newer.reserved_for, newer.attempts) == ("queued", DEVICE, 0)
    assert p.messages[a["id"]].status == "queued"
    fresh = Conn(session_id="fresh")
    clock.t += 1
    p.on_session_open(DEVICE, fresh)
    await p.on_invoke(fresh)
    await p._delivery
    assert speaker.calls == [(b["id"], True)]


async def test_overlapping_polls_keep_each_invoke_to_its_own_lease():
    clock = Clock()
    speaker = Speaker()
    p = make(clock, speaker)
    _, a = p.submit(fields(key="a"), "ha")
    a_item = p.reserve(DEVICE)
    a_binding = f"{wp.INVOKE_TEXT}:{a_item.id}:{a_item.lease}"
    clock.t += 1
    again = p.reserve(DEVICE)
    conn = Conn()
    clock.t += 1
    p.on_session_open(DEVICE, conn)
    await p.on_invoke(conn, a_binding)
    assert await drained(conn) and p._delivery is None and again.status == "queued"


async def test_invoke_delivers_the_reserved_message_with_its_urgency():
    clock = Clock()
    speaker = Speaker()
    p = make(clock, speaker)
    _, normal = p.submit(fields(key="a", ttl=wp.MIN_TTL_SECONDS), "ha")
    polled = p.reserve(DEVICE)
    clock.t += wp.MIN_TTL_SECONDS
    _, urgent = p.submit(fields(key="b", urgent=True, message="Leak!"), "ha")
    conn = Conn()
    clock.t += 1
    p.on_session_open(DEVICE, conn)
    await p.on_invoke(conn)
    await p._delivery
    assert polled.urgent is False and speaker.calls == [(normal["id"], True)]
    assert p.messages[urgent["id"]].status == "queued"


def test_reserved_message_is_held_from_other_devices_and_in_session_delivery():
    p = make(auth=wp.DeviceAuth(True, [DEVICE, OTHER]))
    p.submit(fields(), "ha")
    item = p.reserve(DEVICE)
    assert p.next_for(OTHER) is None and p.reserve(OTHER) is None
    assert p.next_for(DEVICE, reserved=False) is None
    assert p.next_for(DEVICE) is item


async def test_invoke_without_a_reservation_closes():
    p = make()
    p.submit(fields(), "ha")
    conn = Conn()
    await p.on_invoke(conn)
    assert await drained(conn) and p._delivery is None and p.queue[0].status == "queued"


async def test_invoke_while_muted_releases_a_normal_reservation():
    p = make()
    p.submit(fields(), "ha")
    item = p.reserve(DEVICE)
    p.set_mute(10)
    conn = Conn()
    p.on_session_open(DEVICE, conn)
    await p.on_invoke(conn)
    assert await drained(conn) and item.status == "queued" and item.reserved_for is None and item.lease is None


# Device identity

def test_device_auth_with_allow_list_admits_listed_devices_only():
    from core.auth import AuthManager

    manager = AuthManager("secret")
    auth = wp.DeviceAuth(True, [DEVICE], manager.verify_token)
    assert auth.device({"device-id": DEVICE}) == DEVICE
    assert auth.device({"device-id": DEVICE.upper()}) is None
    assert auth.device({"device-id": OTHER}) is None
    token = manager.generate_token("client-1", OTHER)
    assert auth.device({"device-id": OTHER, "client-id": "client-1", "authorization": f"Bearer {token}"}) is None
    assert auth.device({}) is None and auth.device(None) is None
    assert wp.DeviceAuth(False, [DEVICE]).device({"device-id": DEVICE}) is None


def test_device_auth_without_allow_list_takes_ota_tokens():
    from core.auth import AuthManager

    manager = AuthManager("secret")
    auth = wp.DeviceAuth(True, [], manager.verify_token)
    token = manager.generate_token("client-1", OTHER)
    good = {"device-id": OTHER, "client-id": "client-1", "authorization": f"Bearer {token}"}
    assert auth.device(good) == OTHER
    assert auth.device({**good, "client-id": "client-2"}) is None
    assert auth.device({**good, "authorization": token}) is None
    assert auth.device({"device-id": OTHER}) is None


def test_device_auth_from_server_config():
    config = {"server": {"auth_key": "k", "auth": {"enabled": True, "allowed_devices": [DEVICE]}}}
    p = make()
    p.configure_auth(config)
    assert p.auth.device({"device-id": DEVICE}) == DEVICE
    assert p.auth.device({"device-id": OTHER}) is None
    p.configure_auth({"server": {"auth": {"enabled": False}}})
    assert p.auth.auth_enable is False


async def test_invoke_from_an_unverified_session_consumes_nothing():
    p = make()
    _, body = p.submit(fields(device=DEVICE), "ha")
    p.reserve(DEVICE)
    spoof = Conn(device=DEVICE)
    spoof.headers = {"device-id": OTHER}
    await p.on_invoke(spoof)
    assert spoof.closed and p._delivery is None
    assert p.messages[body["id"]].status == "queued"


# Claims (one delivery per message)

async def test_overlapping_invokes_deliver_once():
    speaker = Speaker()
    p = make(speaker=speaker)
    _, body = p.submit(fields(), "ha")
    p.reserve(DEVICE)
    first, second = Conn(session_id="s1"), Conn(session_id="s2")
    p.on_session_open(DEVICE, first)
    p.on_session_open(DEVICE, second)
    await p.on_invoke(first)
    await p.on_invoke(second)
    assert p.messages[body["id"]].status == "delivering"
    await p._delivery
    assert speaker.calls == [(body["id"], True)] and p.messages[body["id"]].attempts == 1
    assert await drained(second)


async def test_dispatch_claims_before_the_task_runs():
    speaker = Speaker()
    p = make(speaker=speaker)
    a, b = Conn(session_id="s1"), Conn(session_id="s2")
    p.on_session_open(DEVICE, a)
    p.on_session_open(DEVICE, b)
    _, body = p.submit(fields(), "ha")
    p.dispatch_once()
    item = p.messages[body["id"]]
    assert item.status == "delivering" and item.attempts == 1
    p.dispatch_once()
    await asyncio.sleep(0.05)
    assert speaker.calls == [(body["id"], False)] and item.status == "delivered"


async def test_a_stale_claim_does_not_finish_the_message():
    gate = asyncio.Event()

    async def slow(conn, item, close_after):
        await gate.wait()
        return "closed"

    p = make(speaker=slow)
    _, body = p.submit(fields(), "ha")
    item = p.messages[body["id"]]
    task = p.claim(Conn(), item)
    item.claim = "newer-owner"
    gate.set()
    await task
    assert item.status == "delivering" and item.attempts == 1


# Delivery

async def test_invoked_announce_is_delivered_and_closes_session():
    speaker = Speaker()
    p = make(speaker=speaker)
    _, body = p.submit(fields(), "ha")
    p.reserve(DEVICE)
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
    assert await drained(conn) and p._delivery is None


async def test_invoke_does_not_block_the_session_loop():
    started = asyncio.Event()

    async def slow(conn, item, close_after):
        started.set()
        await asyncio.sleep(10)
        return "spoken"

    p = make(speaker=slow)
    p.submit(fields(), "ha")
    p.reserve(DEVICE)
    conn = Conn()
    p.on_session_open(DEVICE, conn)
    await asyncio.wait_for(p.on_invoke(conn), 0.1)
    await asyncio.wait_for(started.wait(), 1)
    p._delivery.cancel()


def test_parse_invoke():
    assert wp.parse_invoke(wp.INVOKE_TEXT) == (None, None)
    assert wp.parse_invoke(wp.INVOKE_TEXT + ":m1:l1") == ("m1", "l1")
    for text in (wp.INVOKE_TEXT + ":", wp.INVOKE_TEXT + ":m1", wp.INVOKE_TEXT + ":m1:", wp.INVOKE_TEXT + "x", "Jarvis", None):
        assert wp.parse_invoke(text) is None


@pytest.mark.parametrize("raised, heard, status", [(ConnectionError("gone"), False, "queued"), (ValueError("x"), True, "partial")])
async def test_errors_before_audio_retry_and_after_it_are_partial(raised, heard, status):
    async def speaker(conn, item, close_after):
        if heard:
            return "spoken"
        raise raised

    p = make(speaker=speaker)
    _, body = p.submit(fields(mode="conversation" if heard else "announce"), "ha")
    item = p.messages[body["id"]]
    conn = Conn()
    if heard:
        conn.dialogue = None
    await p.claim(conn, item)
    assert (item.status, item.attempts) == (status, 1)
    if heard:
        assert item.error == "error after audio started"


async def test_failed_delivery_requeues_then_fails():
    speaker = Speaker(outcome="closed")
    p = make(speaker=speaker)
    _, body = p.submit(fields(), "ha")
    item = p.messages[body["id"]]
    for attempt in range(1, wp.MAX_ATTEMPTS):
        await p.claim(Conn(), item)
        assert (item.status, item.attempts, item.claim) == ("queued", attempt, None)
    await p.claim(Conn(), item)
    assert item.status == "failed" and item.error == "closed"
    assert not p.queue


@pytest.mark.parametrize("outcome", ["no_audio", "send_failed", "superseded", "no_tts", "timeout", "error"])
async def test_failures_before_any_audio_requeue(outcome):
    p = make(speaker=Speaker(outcome=outcome))
    _, body = p.submit(fields(), "ha")
    item = p.messages[body["id"]]
    await p.claim(Conn(), item)
    assert (item.status, item.attempts) == ("queued", 1)


async def test_retries_wait_for_the_backoff():
    clock = Clock()
    p = make(clock=clock, speaker=Speaker(outcome="no_audio"))
    _, body = p.submit(fields(), "ha")
    item = p.messages[body["id"]]
    for delay in wp.RETRY_BACKOFF_SECONDS:
        await p.claim(Conn(), item)
        assert item.status == "queued"
        clock.t += delay - 0.1
        assert p.next_for(DEVICE) is None and p.reserve(DEVICE) is None
        clock.t += 0.1
        assert p.next_for(DEVICE) is item
    await p.claim(Conn(), item)
    assert (item.status, item.error, item.attempts) == ("failed", "no_audio", wp.MAX_ATTEMPTS)


async def test_synthesis_failure_after_audio_finishes_as_partial():
    p = make(speaker=Speaker(outcome="incomplete"))
    _, body = p.submit(fields(), "ha")
    item = p.messages[body["id"]]
    await p.claim(Conn(), item)
    assert (item.status, item.error, item.attempts) == ("partial", "synthesis failed after audio started", 1)
    assert not p.queue


async def test_audio_cut_off_finishes_as_partial_without_retry():
    p = make(speaker=Speaker(outcome="partial"))
    _, body = p.submit(fields(urgent=True), "ha")
    item = p.messages[body["id"]]
    await p.claim(Conn(), item)
    assert item.status == "partial" and item.attempts == 1 and not p.queue


async def test_interrupted_announcement_counts_as_delivered():
    p = make(speaker=Speaker(outcome="interrupted"))
    _, body = p.submit(fields(), "ha")
    await p.claim(Conn(), p.messages[body["id"]])
    assert p.messages[body["id"]].status == "delivered"


async def test_conversation_reply_is_recorded():
    p = make()
    p.reply_window = 1
    _, body = p.submit(fields(mode="conversation", message="Shall I close the valve?"), "ha")
    p.reserve(DEVICE)
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
    p.reserve(DEVICE)
    conn = Conn()
    p.on_session_open(DEVICE, conn)
    await p.on_invoke(conn)
    await p._delivery
    assert speaker.calls == [(body["id"], False)]
    assert p.messages[body["id"]].status == "no_reply"
    assert conn.closed


async def test_reply_window_extends_while_user_is_speaking():
    p = make()
    p.reply_window = 0.2
    _, body = p.submit(fields(mode="conversation"), "ha")
    p.reserve(DEVICE)
    conn = Conn()
    p.on_session_open(DEVICE, conn)
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
    stopped = Conn()
    stopped.stop_event.set()
    p.on_session_open(DEVICE, stopped)
    p.dispatch_once()
    await asyncio.sleep(0)
    assert speaker.calls == []


# Upstream hooks

async def test_register_sessions_tracks_verified_open_session():
    p = make()
    seen = []

    async def handle_connection(self, ws):
        seen.append({d: [c._weixue_ws for c in cs] for d, cs in p.sessions.items()})

    wrapped = weixue_patches.register_sessions(handle_connection, p)
    ws = types.SimpleNamespace(request=types.SimpleNamespace(headers={"device-id": DEVICE}))
    await wrapped(Conn(), ws)
    spoof = types.SimpleNamespace(request=types.SimpleNamespace(headers={"device-id": OTHER}))
    await wrapped(Conn(), spoof)
    assert seen == [{DEVICE: [ws]}, {}] and p.sessions == {}


async def test_intercept_invoke_only_for_invoke_text():
    calls = []

    class FakeProactive:
        async def on_invoke(self, conn, text):
            calls.append(text)

    async def handle(self, conn, msg_json):
        calls.append(msg_json["text"])

    wrapped = weixue_patches.intercept_invoke(handle, FakeProactive(), wp.INVOKE_TEXT)
    conn = Conn()
    await wrapped(None, conn, {"type": "listen", "state": "detect", "text": wp.INVOKE_TEXT, "mode": "realtime"})
    await wrapped(None, conn, {"type": "listen", "state": "detect", "text": wp.INVOKE_TEXT + ":m:l"})
    await wrapped(None, conn, {"type": "listen", "state": "detect", "text": wp.INVOKE_TEXT + "x"})
    await wrapped(None, conn, {"type": "listen", "state": "detect", "text": "Jarvis"})
    assert calls == [wp.INVOKE_TEXT, wp.INVOKE_TEXT + ":m:l", wp.INVOKE_TEXT + "x", "Jarvis"]
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
    assert (await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE})).status == 204
    created = await (await client.post("/weixue/proactive/messages", json={"message": "hi"}, headers=AUTH)).json()
    resp = await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE})
    assert resp.status == 200
    body = await resp.json()
    item = client.proactive.messages[created["id"]]
    assert body == {"pending": True, "speak": True, "urgent": False, "id": created["id"], "lease": item.lease}
    assert item.reserved_for == DEVICE and len(item.lease) == 32


async def test_http_poll_reports_urgent(client):
    await client.post("/weixue/proactive/messages", json={"message": "leak", "urgent": True}, headers=AUTH)
    resp = await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE})
    assert resp.status == 200 and (await resp.json())["urgent"] is True


@pytest.mark.parametrize("headers", [{}, {"Device-Id": OTHER}, {"Device-Id": " "}, AUTH])
async def test_http_poll_refuses_unverified_devices(client, headers):
    await client.post("/weixue/proactive/messages", json={"message": "hi"}, headers=AUTH)
    resp = await client.get("/weixue/proactive/poll", headers=headers)
    assert resp.status == 401
    assert client.proactive.queue[0].reserved_for is None and OTHER not in client.proactive.last_poll


async def test_http_poll_disabled_without_server_auth(client):
    client.proactive.auth = wp.DeviceAuth(False, [DEVICE])
    resp = await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE})
    assert resp.status == 503


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
        record = conn.weixue_playback[conn.sentence_id]
        record.frames = 3
        record.done.set()

    asyncio.create_task(finish())
    outcome = await wp.speak(conn, p.messages[body["id"]], close_after=False)
    assert outcome == "spoken" and sent == ["start"]
    assert conn.weixue_playback == {}
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


async def test_speak_marks_the_sentence_of_an_agent_item(monkeypatch):
    import queue

    import core.handle.sendAudioHandle as send_audio

    seen = []

    async def send_tts_message(conn, state, text=None):
        seen.append((state, getattr(conn, "_weixue_ack_sentence", None), conn.sentence_id))

    class TTS:
        def __init__(self):
            self.tts_text_queue = queue.Queue()

        def store_tts_text(self, sentence_id, text):
            pass

        def tts_one_sentence(self, conn, content_type, content_detail=None, sentence_id=None):
            pass

    monkeypatch.setattr(send_audio, "send_tts_message", send_tts_message)
    p = make()
    _, body = p.submit({**fields(message="Lights are on."), "agent": {"speech": "sp1"}}, "weixue-agent")
    item = p.messages[body["id"]]
    conn = Conn()
    conn.tts = TTS()

    async def finish():
        await asyncio.sleep(0.2)
        record = conn.weixue_playback[conn.sentence_id]
        record.frames = 3
        record.done.set()

    asyncio.create_task(finish())
    assert await wp.speak(conn, item, close_after=False) == "spoken"
    assert seen == [("start", item.agent["sentence_id"], item.agent["sentence_id"])]
    assert conn._weixue_ack_sentence is None


# Monitor read scope and summary fields

MONITOR = "m" * 40


async def test_monitor_token_reads_status_only(client):
    client.proactive.monitor_token = MONITOR
    monitor = {"Authorization": f"Bearer {MONITOR}"}
    status = await client.get("/weixue/proactive", headers=monitor)
    assert status.status == 200 and "devices" in await status.json()
    assert (await client.post("/weixue/proactive/messages", json={"message": "hi"}, headers=monitor)).status == 401
    assert (await client.get("/weixue/proactive/messages/x", headers=monitor)).status == 401
    assert (await client.put("/weixue/proactive/mute", json={"minutes": 1}, headers=monitor)).status == 401
    client.proactive.token = ""
    assert (await client.get("/weixue/proactive", headers=monitor)).status == 200
    assert (await client.post("/weixue/proactive/messages", json={"message": "hi"}, headers=monitor)).status == 503


async def test_monitor_token_unset_is_refused(client):
    assert (await client.get("/weixue/proactive", headers={"Authorization": "Bearer "})).status == 401


async def test_summary_reports_held_poll_and_last_poll_end(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    clock = Clock()
    p = make(clock=clock)
    assert p.summary()["devices"] == {}
    waiter = asyncio.create_task(p.poll(DEVICE))
    await asyncio.sleep(0.05)
    device = p.summary()["devices"][DEVICE]
    assert device["poll_held"] is True and device["last_poll_end_seconds_ago"] is None
    clock.t += 1
    assert await asyncio.wait_for(waiter, 2) is None
    clock.t += 2.5
    device = p.summary()["devices"][DEVICE]
    assert device["poll_held"] is False and device["last_poll_end_seconds_ago"] == 2.5


def test_session_open_and_close_reach_the_agent_gateway():
    events = []
    p = make()
    p.agent = types.SimpleNamespace(on_connection=lambda device, kind, conn: events.append((device, kind, conn.session_id)))
    conn = Conn()
    p.on_session_open(DEVICE, conn)
    p.on_session_close(DEVICE, conn)
    p.on_session_close(DEVICE, conn)
    assert events == [(DEVICE, "open", "session-1"), (DEVICE, "close", "session-1")]
