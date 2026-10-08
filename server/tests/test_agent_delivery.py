"""weixue_agent_delivery: mirror, push and resend, poll batches and snapshots, device events and
dedup, speech through the proactive queue with playback acks, tts tagging, routes and the
agent-service stream parser; fake sessions in place of ConnectionHandler."""
import asyncio
import json
import threading
import time
import types

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import weixue_agent_delivery as wad
import weixue_agent_msg as wire
import weixue_proactive as wp

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
DEVICE = "7c:0c:5f:42:4b:fc"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Socket:
    def __init__(self):
        self.sent = []
        self.request = types.SimpleNamespace(headers={"device-id": DEVICE})

    async def send(self, data):
        self.sent.append(data)

    def payloads(self):
        return [json.loads(s)["payload"] for s in self.sent if s.startswith('{"type":"custom"')]


class Conn:
    def __init__(self, device=DEVICE):
        self.headers = {"device-id": device}
        self.websocket = Socket()
        self.websocket.request.headers = {"device-id": device}
        self.session_id = "s1"
        self.stop_event = threading.Event()
        self.sentence_id = None
        self.closed = False

    async def close(self):
        self.closed = True
        self.stop_event.set()


def make(clock=None, wall=None):
    clock = clock or Clock()
    proactive = wp.Proactive(TOKEN, clock=clock, wall=wall or (lambda: 1_790_000_000.0),
                             auth=wp.DeviceAuth(True, [DEVICE]))
    gateway = wad.AgentGateway(proactive, clock=clock, wall=wall or (lambda: 1_790_000_000.0))
    proactive.agent = gateway
    return proactive, gateway


def card(task="t1", state="working", **extra):
    fields = {"chat_id": "c1", "task_id": task, "state": state, "summary": f"{task} {state}"}
    fields.update(extra)
    return fields


async def settle():
    for _ in range(5):
        await asyncio.sleep(0)


def response(seq, kind, boot=7, **extra):
    return {"v": 1, "boot": boot, "seq": seq, "kind": kind, **extra}


APPROVAL = [{"id": "approve", "label": "Approve"}, {"id": "deny", "label": "Deny"}]


# Seq, mirror, push and resend

async def test_seq_monotonic_and_mirror_keeps_latest_per_task():
    _, gw = make()
    a = gw.submit(DEVICE, card("t1"))
    b = gw.submit(DEVICE, card("t1", "needs_input", card_id="i1", options=[{"id": "x", "label": "X"}]))
    assert b["seq"] == a["seq"] + 1 and a["seq"] == 1_790_000_000_000
    stale = gw.submit(DEVICE, card("t1", "done"), seq=a["seq"] - 5)
    dev = gw.devices[DEVICE]
    assert list(dev.mirror) == ["t1"] and dev.mirror["t1"].seq == b["seq"]
    assert stale["seq"] == a["seq"] - 5 and len(dev.outgoing) == 3


async def test_push_to_agent_session_then_shown_ack_and_resend():
    clock = Clock()
    _, gw = make(clock)
    conn = Conn()
    gw.on_agent_session(DEVICE, conn)
    await settle()
    first = conn.websocket.payloads()
    assert first == [{"kind": "agent_sync", "v": 1, "seq": 1_790_000_000_000, "task_ids": []}]
    rec = gw.submit(DEVICE, card("t1"))
    await settle()
    assert conn.websocket.payloads()[-1]["seq"] == rec["seq"]
    clock.t += wad.RESEND_SECONDS
    gw._resend()
    await settle()
    assert [p["seq"] for p in conn.websocket.payloads()].count(rec["seq"]) == 2
    gw.on_device_event(conn, wad.RESPONSE_METHOD, response(1, "shown", ack_upto=rec["seq"]))
    assert gw.devices[DEVICE].outgoing == []
    assert gw.sink.acks[-1]["outcome"] == "shown" and gw.sink.acks[-1]["seq"] == rec["seq"]
    clock.t += 10
    gw._resend()
    await settle()
    assert [p["seq"] for p in conn.websocket.payloads()].count(rec["seq"]) == 2


async def test_unacked_after_max_sends_falls_back_to_snapshot():
    clock = Clock()
    _, gw = make(clock)
    conn = Conn()
    gw.on_agent_session(DEVICE, conn)
    gw.submit(DEVICE, card("t1"))
    for _ in range(wad.MAX_SENDS):
        clock.t += wad.RESEND_SECONDS
        gw._resend()
    dev = gw.devices[DEVICE]
    assert dev.outgoing == [] and dev.snapshot_due
    gw.submit(DEVICE, card("t2"))
    await settle()
    kinds = [p["kind"] for p in conn.websocket.payloads()]
    assert kinds[-1] == "agent_sync" and not dev.snapshot_due
    assert conn.websocket.payloads()[-1]["task_ids"] == ["t1", "t2"]


# Poll

async def test_poll_batches_items_and_header_acks():
    _, gw = make()
    assert not gw.poll_ready(DEVICE)
    gw.on_poll(DEVICE, None)
    gw.submit(DEVICE, card("t1"))
    assert not gw.poll_ready(DEVICE), "a poll without the header is a device without the agent feature"
    gw.on_poll(DEVICE, "1")
    assert gw.poll_ready(DEVICE)
    items = gw.poll_items(DEVICE)
    assert [i["task_id"] for i in items] == ["t1"]
    gw.on_poll(DEVICE, str(items[0]["seq"]))
    assert not gw.poll_ready(DEVICE) and gw.devices[DEVICE].outgoing == []
    assert gw.sink.acks[-1]["outcome"] == "shown" and gw.sink.acks[-1]["via"] == "poll"


async def test_poll_seq_zero_sends_snapshot_with_sync_within_limits():
    _, gw = make()
    for i in range(8):
        gw.submit(DEVICE, card(f"t{i}", summary="x" * 60, title="y" * 24))
    gw.on_poll(DEVICE, "0")
    first = gw.poll_items(DEVICE)
    body = {"pending": True, "speak": False, "urgent": False, "id": None, "agent": first}
    assert len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()) <= wire.MAX_POLL_BYTES
    assert len(first) <= wire.MAX_POLL_ITEMS
    if first[-1]["kind"] != "agent_sync":
        gw.on_poll(DEVICE, str(first[-1]["seq"]))
        rest = gw.poll_items(DEVICE)
        assert rest[-1]["kind"] == "agent_sync" and rest[0]["seq"] > first[-1]["seq"]
        first = first + rest
    assert sorted(p["task_id"] for p in first if p["kind"] == "agent") == [f"t{i}" for i in range(8)]
    assert first[-1]["task_ids"] == [f"t{i}" for i in range(8)]
    gw.on_poll(DEVICE, str(first[-1]["seq"]))
    assert not gw.poll_ready(DEVICE)


async def test_drop_removes_task_and_next_poll_syncs():
    _, gw = make()
    gw.submit(DEVICE, card("keep"))
    gw.submit(DEVICE, card("gone"))
    gw.on_poll(DEVICE, "1")
    items = gw.poll_items(DEVICE)
    gw.on_poll(DEVICE, str(items[-1]["seq"]))
    assert gw.drop(DEVICE, ["gone", "never"]) == ["gone"]
    assert gw.poll_ready(DEVICE)
    snap = gw.poll_items(DEVICE)
    assert snap[-1] == {"kind": "agent_sync", "v": 1, "seq": snap[-1]["seq"], "task_ids": ["keep"]}


async def test_raw_items_are_sent_once():
    _, gw = make()
    gw.on_poll(DEVICE, "5")
    gw.submit(DEVICE, {"kind": "agent", "v": 1, "seq": 99, "pad": "x" * 2100}, raw=True)
    assert len(gw.poll_items(DEVICE)) == 1
    gw.on_poll(DEVICE, "5")
    assert gw.poll_items(DEVICE) == [] and gw.devices[DEVICE].outgoing == []


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setattr(wp, "POLL_HOLD_SECONDS", 0.3)
    proactive, gw = make(clock=time.monotonic, wall=time.time)
    app = web.Application()
    wp.add_routes(app, proactive)
    async with TestClient(TestServer(app)) as c:
        c.proactive, c.gateway = proactive, gw
        yield c
    proactive._dispatcher.cancel()
    gw._runner.cancel()


async def test_http_fixture_route_and_cards_only_poll(client):
    headers = {"Device-Id": DEVICE, "X-Weixue-Agent-Seq": "0"}
    resp = await client.get("/weixue/proactive/poll", headers=headers)
    body = await resp.json()
    assert resp.status == 200 and body["speak"] is False and body["agent"][-1]["kind"] == "agent_sync"
    seq = body["agent"][-1]["seq"]
    assert (await client.post("/weixue/agent/messages", json={"device": DEVICE, "items": [card()]})).status == 401
    resp = await client.post("/weixue/agent/messages", headers=AUTH, json={
        "device": DEVICE, "items": [card("t1", "needs_approval", card_id="i1", options=[{"id": "approve", "label": "Approve"}],
                                         debug_reply={"kind": "approve", "option_id": "approve"})]})
    assert resp.status == 202
    resp = await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE, "X-Weixue-Agent-Seq": str(seq)})
    body = await resp.json()
    assert body["speak"] is False and body["id"] is None and body["agent"][0]["debug_reply"]["kind"] == "approve"
    resp = await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE})
    assert resp.status == 204, "a device without the header gets no cards"
    bad = await client.post("/weixue/agent/messages", headers=AUTH, json={"device": DEVICE, "items": [card(state="nope")]})
    assert bad.status == 400
    view = await (await client.get("/weixue/agent/deliveries", headers=AUTH)).json()
    assert view["devices"][DEVICE]["mirror"][0]["task_id"] == "t1"


async def test_http_poll_carries_speech_and_cards_together(client):
    await client.post("/weixue/proactive/messages", json={"message": "hi"}, headers=AUTH)
    client.gateway.submit(DEVICE, card("t1"))
    resp = await client.get("/weixue/proactive/poll", headers={"Device-Id": DEVICE, "X-Weixue-Agent-Seq": "3"})
    body = await resp.json()
    assert body["speak"] is True and body["id"] and [p["task_id"] for p in body["agent"]] == ["t1"]


# Device events

async def test_device_events_dedup_validation_and_user_ack():
    _, gw = make()
    gw.submit(DEVICE, card("t1", "needs_approval", card_id="i1", options=APPROVAL))
    conn = Conn()
    params = response(4, "approve", chat_id="c1", task_id="t1", card_id="i1", via="loopback")
    gw.on_device_event(conn, wad.RESPONSE_METHOD, params)
    gw.on_device_event(conn, wad.RESPONSE_METHOD, params)
    assert len(gw.sink.responses) == 1 and gw.sink.responses[0]["via"] == "loopback"
    gw.on_device_event(conn, wad.RESPONSE_METHOD, dict(params, boot=8))
    assert len(gw.sink.responses) == 2, "a new boot is a new event"
    gw.on_device_event(conn, wad.RESPONSE_METHOD, response(5, "ack_seen", chat_id="c1", task_id="t1"))
    assert gw.sink.acks[-1]["outcome"] == "user_ack"
    gw.on_device_event(conn, wad.RESPONSE_METHOD, response(6, "approve"))
    assert len(gw.sink.responses) == 3, "invalid response rejected"
    stranger = Conn(device="aa:aa:aa:aa:aa:aa")
    gw.on_device_event(stranger, wad.RESPONSE_METHOD, response(9, "cancel", chat_id="c1"))
    assert len(gw.sink.responses) == 3, "unverified session ignored"


async def test_hello_registers_agent_sessions_only():
    _, gw = make()

    async def hello(_self, conn, msg):
        return None

    wrapped = wad.register_agent_sessions(hello, gw)
    plain = Conn()
    await wrapped(None, plain, {"type": "hello", "features": {"mcp": True}})
    agent = Conn()
    await wrapped(None, agent, {"type": "hello", "features": {"mcp": True, "agent": 1}})
    await settle()
    assert plain.websocket.sent == [] and gw.devices[DEVICE].sessions == [agent]
    assert agent.websocket.payloads()[-1]["kind"] == "agent_sync"


async def test_route_device_events_passes_through():
    _, gw = make()
    calls = []

    async def inner(_self, conn, msg):
        calls.append(msg)

    wrapped = wad.route_device_events(inner, gw)
    msg = {"type": "mcp", "payload": {"jsonrpc": "2.0", "method": wad.RESPONSE_METHOD,
                                       "params": response(1, "cancel", chat_id="c1")}}
    await wrapped(None, Conn(), msg)
    assert calls == [msg] and gw.sink.responses[0]["kind"] == "cancel"


# Speech

def tagged_message(proactive, speech_id):
    return next(m for m in proactive.messages.values() if (m.agent or {}).get("speech") == speech_id)


async def test_spoken_item_queues_proactive_message_with_delivery_key():
    proactive, gw = make()
    rec = gw.submit(DEVICE, card("t1", "unread", delivery="interrupt", speaks=True), speech="Lights are on.")
    msg = tagged_message(proactive, rec["speech_id"])
    assert msg.urgent and msg.device == DEVICE and msg.text == "Lights are on."
    assert msg.key == f"{rec['delivery_id']}:1"
    assert [a["outcome"] for a in gw.sink.acks] == ["accepted"]
    silent = gw.submit(DEVICE, card("t2", delivery="silent", speaks=True), speech="never")
    assert "speech_id" not in silent


async def test_played_complete_cut_and_lost():
    clock = Clock()
    proactive, gw = make(clock)
    rec = gw.submit(DEVICE, card("t1", "unread", speaks=True), speech="Done.")
    msg = tagged_message(proactive, rec["speech_id"])
    msg.agent["sentence_id"] = "s-1"
    gw.on_tts("s-1", "start")
    gw.on_tts("s-1", "stop")
    gw.on_device_event(Conn(), wad.PLAYED_METHOD, {"v": 1, "boot": 1, "seq": 1, "sentence_id": "s-1", "complete": True})
    assert [a["outcome"] for a in gw.sink.acks] == ["accepted", "spoken", "played"]

    rec2 = gw.submit(DEVICE, card("t2", "unread", speaks=True), speech="Cut.")
    tagged_message(proactive, rec2["speech_id"]).agent["sentence_id"] = "s-2"
    gw.on_device_event(Conn(), wad.PLAYED_METHOD, {"v": 1, "boot": 1, "seq": 2, "sentence_id": "s-2",
                                                   "complete": False, "cut_at_ms": 1840, "reason": "wake"})
    cut = gw.sink.acks[-1]
    assert cut["outcome"] == "cut_off" and cut["cut_at_ms"] == 1840 and cut["reason"] == "wake"

    rec3 = gw.submit(DEVICE, card("t3", "unread", speaks=True), speech="Lost.")
    msg3 = tagged_message(proactive, rec3["speech_id"])
    msg3.agent["sentence_id"] = "s-3"
    gw.on_tts("s-3", "stop")
    msg3.status = "delivered"
    clock.t += wad.LOST_SECONDS - 1
    gw._track_speech()
    assert gw.speeches[rec3["speech_id"]].state == "handed_off"
    clock.t += 1
    gw._track_speech()
    assert gw.speeches[rec3["speech_id"]].state == "lost" and gw.sink.acks[-1]["outcome"] == "lost"


async def test_failed_speech_before_audio_is_resubmitted_with_new_key():
    proactive, gw = make()
    rec = gw.submit(DEVICE, card("t1", "unread", speaks=True), speech="Again.")
    first = tagged_message(proactive, rec["speech_id"])
    first.status = "failed"
    first.error = "no_audio"
    gw._track_speech()
    second = [m for m in proactive.messages.values() if (m.agent or {}).get("speech") == rec["speech_id"] and m is not first]
    assert second and second[0].key == f"{rec['delivery_id']}:2"
    assert gw.sink.acks[-2]["outcome"] == "deferred"


async def test_await_played_holds_until_ack_or_8s_after_stop(monkeypatch):
    clock = Clock()
    proactive, gw = make(clock)
    rec = gw.submit(DEVICE, card("t1", "unread", speaks=True), speech="Hold.")
    msg = tagged_message(proactive, rec["speech_id"])
    msg.agent["sentence_id"] = "s-1"
    conn = Conn()
    task = asyncio.get_running_loop().create_task(gw.await_played(msg, conn))
    await asyncio.sleep(0.15)
    assert not task.done()
    gw.on_tts("s-1", "stop")
    clock.t += wad.HOLD_AFTER_STOP_SECONDS
    await asyncio.wait_for(task, 1)
    rec2 = gw.submit(DEVICE, card("t2", "unread", speaks=True), speech="Hold.")
    msg2 = tagged_message(proactive, rec2["speech_id"])
    msg2.agent["sentence_id"] = "s-2"
    task = asyncio.get_running_loop().create_task(gw.await_played(msg2, conn))
    await asyncio.sleep(0.15)
    gw.on_device_event(conn, wad.PLAYED_METHOD, {"v": 1, "boot": 1, "seq": 3, "sentence_id": "s-2", "complete": True})
    await asyncio.wait_for(task, 1)


async def test_proactive_deliver_holds_tagged_session_then_closes():
    clock = Clock()
    calls = []

    async def speaker(conn, item, close_after):
        calls.append(close_after)
        item.agent["sentence_id"] = "s-9"
        gw.on_device_event(conn, wad.PLAYED_METHOD, {"v": 1, "boot": 1, "seq": 1, "sentence_id": "s-9", "complete": True})
        return "spoken"

    proactive, gw = make(clock)
    proactive.speaker = speaker
    rec = gw.submit(DEVICE, card("t1", "unread", speaks=True), speech="Bye.")
    msg = tagged_message(proactive, rec["speech_id"])
    conn = Conn()
    conn._weixue_invoked = True
    conn._weixue_busy = False
    msg.status = "delivering"
    msg.claim = "c"
    await proactive.deliver(conn, msg, "c")
    assert calls == [False] and conn.closed and msg.status == "delivered"


# tts tagging

async def test_tts_frames_of_the_agent_sentence_carry_sentence_id_and_ack():
    _, gw = make()

    async def send_tts_message(conn, state, text=None):
        message = {"type": "tts", "state": state, "session_id": "s1"}
        if text is not None:
            message["text"] = text
        if state == "stop":
            conn.client_is_speaking = False
        await conn.websocket.send(json.dumps(message))

    wrapped = wad.tag_tts_messages(send_tts_message, gw)
    conn = Conn()
    conn.sentence_id = "abc"
    await wrapped(conn, "start")
    conn._weixue_ack_sentence = "abc"
    await wrapped(conn, "start")
    await wrapped(conn, "sentence_start", "Hello")
    conn.client_is_speaking = True
    await wrapped(conn, "stop")
    sent = [json.loads(s) for s in conn.websocket.sent]
    assert "ack" not in sent[0]
    assert all(m["sentence_id"] == "abc" and m["ack"] is True for m in sent[1:])
    assert conn.client_is_speaking is False, "attribute writes reach the connection"
    conn.sentence_id = "other"
    await wrapped(conn, "start")
    assert "ack" not in json.loads(conn.websocket.sent[-1])


# Live source

async def test_offer_maps_agent_deliveries_and_drops_other_kinds():
    _, gw = make()
    rec = gw.offer(DEVICE, {"delivery_id": "dlv_1", "lease_id": "l1", "kind": "agent", "tier": "SILENT",
                            "chat_id": "cht_1", "task_id": "tsk_1",
                            "payload": {"state": "progress", "summary": "Working"}})
    assert rec["delivery_id"] == "dlv_1" and gw.devices[DEVICE].mirror["tsk_1"].lease_id == "l1"
    assert gw.offer(DEVICE, {"delivery_id": "dlv_2", "lease_id": "l2", "kind": "chats", "tier": "SILENT"}) is None
    assert gw.sink.acks[-1]["outcome"] == "dropped"


async def test_parse_sse():
    async def lines():
        for line in (b": hb\n", b"\n", b"id: 1\n", b"event: delivery\n", b'data: {"delivery_id": "dlv_1"}\n', b"\n",
                     b"event: other\n", b"data: {}\n", b"\n"):
            yield line

    assert [o async for o in wad.parse_sse(lines())] == [{"delivery_id": "dlv_1"}]


async def test_service_sink_posts_acks_and_routes():
    posts = []

    class Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class Session:
        def __init__(self, headers=None):
            self.headers = headers

        def post(self, url, json=None, headers=None):
            posts.append((url, json, headers))
            return Resp()

    sink = wad.ServiceSink(wad.setup_logging(), "http://agent:8010", "tok", session_factory=Session)
    sink.ack("dlv_1", "lease-1", "played", DEVICE, sentence_id="s1")
    sink.ack("fx_1", "lease-1", "played", DEVICE)
    sink.response(DEVICE, {"kind": "approve", "boot": 1, "seq": 2, "chat_id": "cht_1", "card_id": "int_1", "via": "tap"})
    await settle()
    assert posts[0][0] == "http://agent:8010/v1/deliveries/dlv_1/ack"
    assert posts[0][1] == {"lease_id": "lease-1", "outcome": "played"}
    assert posts[1][0] == "http://agent:8010/v1/chats/cht_1/approve"
    assert posts[1][2] == {"Idempotency-Key": wire.decision_id(DEVICE, 1, 2)} and len(posts) == 2


def test_apply_installs_hooks_once(monkeypatch):
    import importlib
    import sys

    class HelloTextMessageHandler:
        async def handle(self, conn, msg_json):
            return None

    class McpTextMessageHandler:
        async def handle(self, conn, msg_json):
            return None

    for name, cls in (("helloMessageHandler", HelloTextMessageHandler), ("mcpMessageHandler", McpTextMessageHandler)):
        module = types.ModuleType(f"core.handle.textHandler.{name}")
        setattr(module, cls.__name__, cls)
        monkeypatch.setitem(sys.modules, f"core.handle.textHandler.{name}", module)

    proactive = wp.Proactive(TOKEN, auth=wp.DeviceAuth(True, [DEVICE]))
    monkeypatch.setattr(wp, "INSTANCE", proactive)
    monkeypatch.setattr(wad, "INSTANCE", None)
    send_audio = importlib.import_module("core.handle.sendAudioHandle")
    mcp = importlib.import_module("core.handle.textHandler.mcpMessageHandler").McpTextMessageHandler
    monkeypatch.setattr(send_audio, "send_tts_message", send_audio.send_tts_message)
    gateway = wad.apply()
    first = (send_audio.send_tts_message, mcp.handle, HelloTextMessageHandler.handle)
    assert wad.apply() is gateway and proactive.agent is gateway
    assert (send_audio.send_tts_message, mcp.handle, HelloTextMessageHandler.handle) == first
    assert first[0].weixue_wrapped is not None and first[1].weixue_agent_wrapped is not None


async def test_mirror_evicts_like_the_device_store_without_a_snapshot():
    _, gw = make()
    gw.submit(DEVICE, card("old-working"))
    gw.submit(DEVICE, card("unseen-failed", "failed", seen=False))
    gw.submit(DEVICE, card("seen-cancelled", "cancelled", seen=True))
    for i in range(5):
        gw.submit(DEVICE, card(f"t{i}", "needs_input", card_id=f"i{i}", options=[{"id": "x", "label": "X"}]))
    gw.submit(DEVICE, card("t9"))
    dev = gw.devices[DEVICE]
    assert len(dev.mirror) == wad.MIRROR_TASKS and "seen-cancelled" not in dev.mirror and not dev.snapshot_due
    gw.submit(DEVICE, card("t10"))
    assert "unseen-failed" not in dev.mirror
    gw.submit(DEVICE, card("t11"))
    assert "old-working" not in dev.mirror and "t0" in dev.mirror


async def test_mirror_never_evicts_open_cards_and_drops_a_new_task_when_full():
    _, gw = make()
    for i in range(wad.MIRROR_TASKS):
        gw.submit(DEVICE, card(f"t{i}", "needs_approval", card_id=f"i{i}", options=APPROVAL))
    rec = gw.submit(DEVICE, card("t9"))
    dev = gw.devices[DEVICE]
    assert rec["dropped"] and "t9" not in dev.mirror and set(dev.mirror) == {f"t{i}" for i in range(8)}
    assert all(e.payload["task_id"] != "t9" for e in dev.outgoing)
    assert gw.sink.acks[-1]["outcome"] == "dropped" and "open cards" in gw.sink.acks[-1]["detail"]
    update = gw.submit(DEVICE, card("t0", "working"))
    assert "dropped" not in update and dev.mirror["t0"].payload["state"] == "working"
    gw.submit(DEVICE, card("t9"))
    assert "t9" in dev.mirror and "t0" not in dev.mirror, "the working task goes before any open card"


async def test_stale_card_decisions_are_not_forwarded():
    _, gw = make()
    gw.submit(DEVICE, card("t1", "needs_approval", card_id="i1", options=APPROVAL))
    gw.submit(DEVICE, card("t1", "needs_input", card_id="i2", options=[{"id": "x", "label": "X"}]))
    conn = Conn()
    gw.on_device_event(conn, wad.RESPONSE_METHOD, response(1, "approve", chat_id="c1", task_id="t1", card_id="i1"))
    gw.on_device_event(conn, wad.RESPONSE_METHOD,
                       response(2, "answer", chat_id="c1", task_id="t1", card_id="i1", option_id="x"))
    assert not gw.sink.responses, "decisions on a superseded card are dropped"
    gw.on_device_event(conn, wad.RESPONSE_METHOD,
                       response(3, "answer", chat_id="c1", task_id="t1", card_id="i2", option_id="x"))
    assert [r["card_id"] for r in gw.sink.responses] == ["i2"]
    gw.submit(DEVICE, card("t1", "working"))
    gw.on_device_event(conn, wad.RESPONSE_METHOD, response(4, "deny", chat_id="c1", task_id="t1", card_id="i2"))
    assert len(gw.sink.responses) == 1, "a card on a task that moved on is stale"
    gw.on_device_event(conn, wad.RESPONSE_METHOD, response(5, "deny", chat_id="c1", task_id="t7", card_id="i9"))
    assert len(gw.sink.responses) == 2, "a task the gateway does not mirror is forwarded"


async def test_answered_debug_reply_is_not_replayed_in_a_snapshot():
    _, gw = make()
    reply = {"kind": "approve", "option_id": "approve"}
    gw.submit(DEVICE, card("t1", "needs_approval", card_id="i1", options=APPROVAL, debug_reply=reply))
    gw.submit(DEVICE, card("t2", "needs_approval", card_id="i2", options=APPROVAL, debug_reply=reply))
    conn = Conn()
    gw.on_device_event(conn, wad.RESPONSE_METHOD,
                       response(1, "approve", chat_id="c1", task_id="t1", card_id="i1", via="loopback"))
    gw.on_poll(DEVICE, "0")
    items = {p["task_id"]: p for p in gw.poll_items(DEVICE) if p["kind"] == "agent"}
    assert "debug_reply" not in items["t1"] and items["t2"]["debug_reply"] == reply
    assert "debug_reply" in gw.devices[DEVICE].mirror["t1"].payload, "the mirror keeps the item as sent"
    gw.on_device_event(conn, wad.RESPONSE_METHOD, response(2, "ack_seen", chat_id="c1", task_id="t2"))
    gw.on_agent_session(DEVICE, conn)
    await settle()
    assert all("debug_reply" not in p for p in conn.websocket.payloads())
