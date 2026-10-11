"""weixue_agent_delivery against a fake agent-service: decision receipts after the service
answered (F04), speech handed off only after the service recorded it and never again after a
restart (F09), ordered and retried acks with one retry owner (F10), the device snapshot restored
before any pruning sync (F11), and INTERRUPT stopping the current playback (F13)."""
import asyncio
import json
import time

import pytest

import weixue_agent_delivery as wad
import weixue_agent_msg as wire
import weixue_proactive as wp
from tests.test_agent_delivery import APPROVAL, DEVICE, TOKEN, Conn, card, response, settle, wait_for

LEASE = "0f8fad5b-d9cb-469f-a165-70867728950e"
LEASE2 = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
LEASE3 = "16fd2706-8baf-433b-82eb-8c7fada847da"


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(wad, "RETRY_FIRST_SECONDS", 0.02)
    monkeypatch.setattr(wad, "ACK_RETRY_FIRST_SECONDS", 0.02)
    monkeypatch.setattr(wad, "DECISION_WINDOW_SECONDS", 0.3)
    monkeypatch.setattr(wad, "ACCEPT_WINDOW_SECONDS", 0.3)
    monkeypatch.setattr(wad, "ACK_RETRY_SECONDS", 0.3)
    monkeypatch.setattr(wad, "ACK_BACKOFF_MAX_SECONDS", 0.05)
    monkeypatch.setattr(wad, "REQUEST_SECONDS", 0.1)


HANG = None


class Service:
    """aiohttp-session stand-in for agent-service: answer(path, body) gives each POST's status
    (default 200, optionally after a delay; delay HANG never answers), GET snapshot returns
    `snapshot` after `snapshot_delay`. A request ends early only when the caller's timeout
    cancels it (counted in `cancelled`)."""

    def __init__(self, answer=None):
        self.answer = answer or (lambda path, body: 200)
        self.posts = []
        self.snapshot = {"device_id": DEVICE, "cards": [], "chats": None}
        self.snapshot_status = 200
        self.snapshot_delay = 0.0
        self.gets = []
        self.cancelled = 0

    def session(self, headers=None):
        return self

    def _resp(self, status, body=None, delay=0.0):
        service = self

        class Resp:
            async def __aenter__(self_inner):
                try:
                    if delay is HANG:
                        await asyncio.Event().wait()
                    elif delay:
                        await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    service.cancelled += 1
                    raise
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

            async def json(self_inner):
                return body

        r = Resp()
        r.status = status
        return r

    def post(self, url, json=None, headers=None):
        path = url.split("8010", 1)[1]
        self.posts.append((path, json, headers))
        answer = self.answer(path, json)
        status, delay = answer if isinstance(answer, tuple) else (answer, 0.0)
        return self._resp(status, delay=delay)

    def get(self, url, params=None):
        path = url.split("8010", 1)[1]
        self.gets.append(path)
        if path.endswith("/snapshot"):
            return self._resp(self.snapshot_status, self.snapshot, self.snapshot_delay)
        return self._resp(200, None)

    def acks(self, delivery_id=None):
        return [(p.split("/")[3], b["outcome"]) for p, b, _ in self.posts
                if p.endswith("/ack") and (delivery_id is None or p.split("/")[3] == delivery_id)]


def gateway(service, state_dir=None, url="", clock=None):
    clock = clock or time.monotonic
    pstate = wp.StateFile(str(state_dir / "proactive.json")) if state_dir else None
    proactive = wp.Proactive(TOKEN, clock=clock, wall=time.time, auth=wp.DeviceAuth(True, [DEVICE]), state=pstate)
    sink = wad.ServiceSink(wad.setup_logging(), "http://agent:8010", "tok", session_factory=service.session)
    gstate = wp.StateFile(str(state_dir / "agent.json")) if state_dir else None
    gw = wad.AgentGateway(proactive, clock=clock, wall=time.time, sink=sink, state=gstate)
    gw.service_url = url
    proactive.agent = gw
    return proactive, gw


def spoken_offer(delivery="dlv_s1", lease=LEASE, attempt=1, gseq=10, tier="WHEN_IDLE", task="tsk_1"):
    return {"delivery_id": delivery, "lease_id": lease, "gseq": gseq, "attempt": attempt, "kind": "agent",
            "tier": tier, "chat_id": "cht_1", "task_id": task, "speak": "The lights are on.",
            "payload": {"state": "done", "summary": "Lights on", "unread": True}}


def receipts(conn):
    return [p for p in conn.websocket.payloads() if p["kind"] == "agent_ack"]


def approval_card(gw):
    gw.submit(DEVICE, card("t1", "needs_approval", chat_id="cht_1", card_id="int_1", options=APPROVAL))


def approve(seq, boot=7, kind="approve"):
    return response(seq, kind, boot=boot, chat_id="cht_1", task_id="t1", card_id="int_1")


# F04 decision receipts


async def test_a_503_never_becomes_accepted_and_a_later_tap_reuses_the_decision_id():
    up = {"ok": False}
    service = Service(lambda path, body: 200 if up["ok"] else 503)
    _, gw = gateway(service)
    approval_card(gw)
    conn = Conn()
    assert gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1)) is None, "no receipt before the service answered"
    assert await wait_for(lambda: receipts(conn), 2)
    assert receipts(conn)[-1]["result"] == "stale"
    decisions = [(p, h["Idempotency-Key"]) for p, _, h in service.posts]
    assert len(decisions) >= 2 and len({k for _, k in decisions}) == 1, "retried with one decision id"
    assert (DEVICE, "int_1") not in gw.card_decisions
    assert gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1)) == receipts(conn)[-1], "a resend gets that receipt"

    up["ok"] = True
    gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(2))
    assert await wait_for(lambda: len(receipts(conn)) == 2, 2)
    assert receipts(conn)[-1]["result"] == "accepted"
    assert service.posts[-1][2]["Idempotency-Key"] == decisions[0][1], "the second tap repeats the same decision"
    dup = gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(3, kind="deny"))
    assert dup["result"] == "duplicate" and dup["decided"] == "approve"


async def test_a_transient_failure_retries_within_the_window():
    answers = iter([0, 503, 200])
    service = Service(lambda path, body: next(answers))
    _, gw = gateway(service)
    approval_card(gw)
    conn = Conn()
    gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1))
    assert await wait_for(lambda: receipts(conn), 2)
    assert receipts(conn)[-1]["result"] == "accepted" and len(service.posts) == 3
    assert gw.card_decisions[(DEVICE, "int_1")][0] == "approve"


async def test_a_refused_decision_is_stale_at_once():
    service = Service(lambda path, body: 409)
    _, gw = gateway(service)
    approval_card(gw)
    conn = Conn()
    gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1))
    assert await wait_for(lambda: receipts(conn), 1)
    assert receipts(conn)[-1]["result"] == "stale" and len(service.posts) == 1


async def test_taps_and_resends_while_a_decision_is_forwarded_get_no_receipt():
    service = Service(lambda path, body: (200, 0.15))
    _, gw = gateway(service)
    approval_card(gw)
    conn = Conn()
    assert gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1)) is None
    assert gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1)) is None, "a resend of the same seq"
    assert gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(2, kind="deny")) is None, "another tap"
    assert await wait_for(lambda: receipts(conn), 1)
    assert [r["result"] for r in receipts(conn)] == ["accepted"] and len(service.posts) == 1
    again = gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(2, kind="deny"))
    assert again["result"] == "duplicate", "the other tap's resend is answered once the first was recorded"


async def test_card_decisions_survive_a_gateway_restart(tmp_path):
    service = Service()
    _, gw = gateway(service, tmp_path)
    approval_card(gw)
    conn = Conn()
    gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1))
    assert await wait_for(lambda: receipts(conn), 1)
    _, again = gateway(service, tmp_path)
    approval_card(again)
    dup = again.on_device_event(Conn(), wad.RESPONSE_METHOD, approve(2, boot=8, kind="deny"))
    assert dup["result"] == "duplicate" and dup["decided"] == "approve" and len(service.posts) == 1


# F09 speech hand-off


async def test_speech_is_queued_only_after_the_service_recorded_the_hand_off():
    queued_at_accept = []
    proactive = None

    def answer(path, body):
        if body.get("outcome") == "accepted":
            queued_at_accept.append(len(proactive.messages))
        return 200

    service = Service(answer)
    proactive, gw = gateway(service)
    rec = gw.offer(DEVICE, spoken_offer())
    speech = gw.speeches[rec["speech_id"]]
    assert speech.state == "accepting" and proactive.messages == {}
    assert await wait_for(lambda: speech.state == "handed_off", 1)
    msg = proactive.messages[speech.proactive_id]
    assert queued_at_accept == [0] and msg.key == "dlv_s1:1"
    accepted = next(b for p, b, _ in service.posts if b.get("outcome") == "accepted")
    assert accepted["proactive_id"] == msg.id and accepted["lease_id"] == LEASE


@pytest.mark.parametrize("status, state", [(503, "unaccepted"), (409, "stale")])
async def test_speech_without_a_recorded_hand_off_is_not_played(status, state):
    service = Service(lambda path, body: status)
    proactive, gw = gateway(service)
    rec = gw.offer(DEVICE, spoken_offer())
    speech = gw.speeches[rec["speech_id"]]
    assert await wait_for(lambda: speech.state == state, 1)
    assert proactive.messages == {} and proactive.queue == wp.deque()
    assert gw.devices[DEVICE].mirror["tsk_1"].delivery_id == "dlv_s1", "the card still goes to the device"


async def test_a_handed_off_attempt_is_never_spoken_again_after_a_restart(tmp_path):
    service = Service()
    proactive, gw = gateway(service, tmp_path)
    rec = gw.offer(DEVICE, spoken_offer())
    assert await wait_for(lambda: gw.speeches[rec["speech_id"]].state == "handed_off", 1)

    proactive2, gw2 = gateway(service, tmp_path)
    again = gw2.offer(DEVICE, spoken_offer(lease=LEASE2))
    assert proactive2.messages and all(m.status in wp.FINAL for m in proactive2.messages.values())
    assert gw2.speeches[again["speech_id"]].state == "lost" and not proactive2.queue
    assert await wait_for(lambda: ("dlv_s1", "lost") in service.acks(), 1)
    lost = [b for p, b, _ in service.posts if b.get("outcome") == "lost"][-1]
    assert lost["lease_id"] == LEASE2 and lost["detail"] == "handed off before"
    later = gw2.offer(DEVICE, spoken_offer(lease=LEASE3, attempt=2))
    assert gw2.speeches[later["speech_id"]].state == "lost", "a later attempt after a lost one stays unspoken"
    assert not proactive2.queue and not [m for m in proactive2.messages.values() if m.key == "dlv_s1:2"]
    _, gw3 = gateway(service, tmp_path)
    third = gw3.offer(DEVICE, spoken_offer(lease=LEASE3, attempt=3))
    assert gw3.speeches[third["speech_id"]].state == "lost" and not gw3.proactive.queue


@pytest.mark.parametrize("restart", [False, True])
async def test_an_attempt_deferred_before_audio_is_spoken_in_the_next_attempt(tmp_path, restart):
    service = Service()
    proactive, gw = gateway(service, tmp_path)
    first = gw.speeches[gw.offer(DEVICE, spoken_offer())["speech_id"]]
    assert await wait_for(lambda: first.state == "handed_off", 1)
    msg = proactive.messages[first.proactive_id]
    msg.status, msg.error = "failed", "device busy"
    gw._track_speech()
    assert first.state == "deferred" and "dlv_s1" not in gw.handoffs
    assert await wait_for(lambda: service.acks("dlv_s1") == [("dlv_s1", "accepted"), ("dlv_s1", "deferred")], 1)
    assert dict(json.loads((tmp_path / "agent.json").read_text())["handoffs"]).get("dlv_s1") is None
    if restart:
        proactive, gw = gateway(service, tmp_path)
    second = gw.speeches[gw.offer(DEVICE, spoken_offer(lease=LEASE2, attempt=2))["speech_id"]]
    assert await wait_for(lambda: second.state == "handed_off", 1), "the service's next attempt is spoken"
    assert proactive.messages[second.proactive_id].key == "dlv_s1:2"


@pytest.mark.parametrize("outcome, kept", [("played", True), ("cut_off", True), ("lost", True),
                                           ("deferred", False), ("dropped", False)])
async def test_a_hand_off_is_kept_after_audio_and_removed_after_an_end_before_audio(tmp_path, outcome, kept):
    service = Service()
    _, gw = gateway(service, tmp_path)
    speech = gw.speeches[gw.offer(DEVICE, spoken_offer())["speech_id"]]
    assert await wait_for(lambda: speech.state == "handed_off", 1)
    assert gw._speech_final(speech, outcome)
    on_disk = dict(json.loads((tmp_path / "agent.json").read_text())["handoffs"])
    if kept:
        assert on_disk["dlv_s1"] == {"attempt": 1, "state": outcome} == gw.handoffs["dlv_s1"]
    else:
        assert "dlv_s1" not in on_disk and "dlv_s1" not in gw.handoffs
    assert not gw._speech_final(speech, "played"), "one terminal ack per attempt"


async def test_an_unaccepted_attempt_leaves_no_hand_off(tmp_path):
    service = Service(lambda path, body: 503)
    _, gw = gateway(service, tmp_path)
    speech = gw.speeches[gw.offer(DEVICE, spoken_offer())["speech_id"]]
    assert await wait_for(lambda: speech.state == "unaccepted", 1)
    assert "dlv_s1" not in gw.handoffs
    assert "dlv_s1" not in dict(json.loads((tmp_path / "agent.json").read_text())["handoffs"])
    assert not gw._speech_final(speech, "played"), "unaccepted is the attempt's end"


# State file


def test_state_file_falls_back_to_the_previous_generation(tmp_path):
    path = tmp_path / "agent.json"
    state = wp.StateFile(str(path))
    assert state.save({"n": 1}) and state.save({"n": 2})
    assert json.loads((tmp_path / "agent.json.prev").read_text()) == {"n": 1}
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text('{"n": 2, "trunc')
    again = wp.StateFile(str(path))
    assert again.load() == {"n": 1}
    assert again.save({"n": 3})
    assert json.loads((tmp_path / "agent.json.prev").read_text()) == {"n": 1}, "a corrupt file is never kept"
    assert wp.StateFile(str(path)).load() == {"n": 3}
    path.unlink()
    assert wp.StateFile(str(path)).load() == {"n": 1}, "a current file lost between the renames"


async def test_a_card_decision_survives_a_corrupt_state_file(tmp_path):
    service = Service()
    _, gw = gateway(service, tmp_path)
    approval_card(gw)
    gw.on_device_event(Conn(), wad.RESPONSE_METHOD, approve(1))
    assert await wait_for(lambda: (DEVICE, "int_1") in gw.card_decisions, 1)
    gw.sink.ack("dlv_x", LEASE, "shown", DEVICE)
    assert await wait_for(lambda: gw.sink.pending_acks == {}, 1)
    (tmp_path / "agent.json").write_text("{")
    _, again = gateway(service, tmp_path)
    assert again.card_decisions[(DEVICE, "int_1")][0] == "approve"


async def test_speech_whose_hand_off_was_not_written_is_not_played(tmp_path):
    service = Service()
    proactive, gw = gateway(service, tmp_path)
    tmp_path.chmod(0o500)
    try:
        speech = gw.speeches[gw.offer(DEVICE, spoken_offer())["speech_id"]]
        assert await wait_for(lambda: speech.state == "deferred", 1)
    finally:
        tmp_path.chmod(0o700)
    assert proactive.messages == {} and not proactive.queue
    assert "dlv_s1" not in gw.handoffs
    assert await wait_for(lambda: service.acks("dlv_s1") == [("dlv_s1", "accepted"), ("dlv_s1", "deferred")], 1)
    deferred = [b for p, b, _ in service.posts if b.get("outcome") == "deferred"][-1]
    assert deferred["detail"] == "hand-off not recorded" and deferred["retry_after_ms"] == wad.DEFERRED_RETRY_MS


async def test_proactive_idempotency_keys_survive_a_restart(tmp_path):
    state = wp.StateFile(str(tmp_path / "proactive.json"))
    first = wp.Proactive(TOKEN, auth=wp.DeviceAuth(True, [DEVICE]), state=state)
    fields = wp.parse_request({"mode": "announce", "message": "Leak under the sink."}, "ha-leak-1")
    status, body = first.submit(fields, "ha")
    assert status == 202
    first.claim(Conn(), first.queue[0])
    first._delivery.cancel()
    second = wp.Proactive(TOKEN, auth=wp.DeviceAuth(True, [DEVICE]), state=wp.StateFile(str(tmp_path / "proactive.json")))
    status, again = second.submit(fields, "ha")
    assert status == 200 and again["duplicate"] and again["id"] == body["id"]
    assert again["status"] == "partial" and "restarted" in again["error"] and not second.queue
    assert (tmp_path / "proactive.json").stat().st_mode & 0o777 == 0o600


# F10 acks


async def test_acks_of_a_delivery_are_posted_in_order_and_retried():
    answers = {"n": 0}

    def answer(path, body):
        answers["n"] += 1
        if body["outcome"] == "shown" and answers["n"] == 1:
            return 503, 0.05
        return 200

    service = Service(answer)
    _, gw = gateway(service)
    gw.sink.ack("dlv_1", LEASE, "shown", DEVICE)
    gw.sink.ack("dlv_1", LEASE, "user_ack", DEVICE)
    assert await wait_for(lambda: len(service.posts) == 3, 1)
    assert [b["outcome"] for _, b, _ in service.posts] == ["shown", "shown", "user_ack"]
    assert gw.sink.pending_acks == {}


async def test_a_refused_ack_is_not_retried():
    service = Service(lambda path, body: 409)
    _, gw = gateway(service)
    gw.sink.ack("dlv_1", LEASE, "played", DEVICE)
    await asyncio.sleep(0.1)
    assert len(service.posts) == 1 and gw.sink.pending_acks == {}


async def test_unsent_acks_survive_a_restart(tmp_path):
    down = Service(lambda path, body: 503)
    _, gw = gateway(down, tmp_path)
    gw.sink.ack("dlv_1", LEASE, "played", DEVICE, sentence_id="s1")
    await asyncio.sleep(0.05)
    doc = json.loads((tmp_path / "agent.json").read_text())
    assert doc["acks"] == [["dlv_1", [{"lease_id": LEASE, "outcome": "played", "sentence_id": "s1"}]]]
    for task in list(gw.sink._ack_workers.values()):
        task.cancel()
    up = Service()
    _, gw2 = gateway(up, tmp_path)
    gw2.start()
    assert await wait_for(lambda: up.acks() == [("dlv_1", "played")], 1)
    assert await wait_for(lambda: json.loads((tmp_path / "agent.json").read_text())["acks"] == [], 1)
    gw2._runner.cancel()


async def test_played_never_follows_a_terminal_ack_and_accepted_never_follows_played():
    service = Service()
    proactive, gw = gateway(service)
    rec = gw.offer(DEVICE, spoken_offer())
    speech = gw.speeches[rec["speech_id"]]
    assert await wait_for(lambda: speech.state == "handed_off", 1)
    proactive.messages[speech.proactive_id].agent["sentence_id"] = "s-1"
    conn = Conn()
    conn._weixue_ack_sentence = "s-1"
    gw.on_interrupted(conn)
    gw.on_device_event(conn, wad.PLAYED_METHOD, {"v": 1, "boot": 1, "seq": 1, "sentence_id": "s-1", "complete": True})
    gw._track_speech()
    assert await wait_for(lambda: len(service.acks("dlv_s1")) == 2, 1)
    await asyncio.sleep(0.05)
    assert service.acks("dlv_s1") == [("dlv_s1", "accepted"), ("dlv_s1", "cut_off")]
    cut = [b for p, b, _ in service.posts if b.get("outcome") == "cut_off"][0]
    assert cut["reason"] == "superseded" and cut["sentence_id"] == "s-1"


# F11 restore


def snapshot_card(delivery, task, state="done", gseq=5, **payload):
    return {"delivery_id": delivery, "gseq": gseq, "lease_id": None, "kind": "agent", "tier": "SILENT",
            "chat_id": "cht_1", "task_id": task, "speak": None, "attempt": 1, "state": "delivered",
            "payload": {"state": state, "summary": f"{task} {state}", "unread": True, **payload}}


async def test_no_pruning_sync_before_the_snapshot_is_restored(tmp_path):
    service = Service()
    service.snapshot["cards"] = [snapshot_card("dlv_a", "tsk_a"),
                                 snapshot_card("dlv_b", "tsk_b", "needs_approval", gseq=7,
                                               interaction={"interaction_id": "int_b", "action": {}}, options=APPROVAL)]
    _, gw = gateway(service, tmp_path, url="http://agent:8010")
    gw.wire_seqs["dlv_a"] = 1_700_000_000_123
    gw._sources[DEVICE] = asyncio.get_running_loop().create_future()
    conn = Conn()
    gw.on_agent_session(DEVICE, conn, 2)
    gw.on_poll(DEVICE, "0", "2")
    await settle()
    assert conn.websocket.payloads() == [] and gw.poll_items(DEVICE) == [] and not gw.poll_ready(DEVICE)
    assert await gw.restore(DEVICE)
    await settle()
    payloads = conn.websocket.payloads()
    assert [p["kind"] for p in payloads] == ["agent", "agent", "agent_sync"]
    assert payloads[-1]["task_ids"] == ["tsk_a", "tsk_b"]
    assert payloads[0]["seq"] == 1_700_000_000_123, "the seq the device applied before the restart"
    assert payloads[1]["card_id"] == "int_b" and "speaks" in payloads[1] and payloads[1]["speaks"] is False
    assert gw.proactive.messages == {}, "restoring never speaks"


async def test_snapshot_failure_keeps_pruning_held_and_the_stream_closed(monkeypatch):
    service = Service()
    service.snapshot_status = 503
    _, gw = gateway(service, url="http://agent:8010")
    gw._sources[DEVICE] = asyncio.get_running_loop().create_future()
    gw.on_poll(DEVICE, "0", "2")
    assert not await gw.restore(DEVICE)
    assert gw.poll_items(DEVICE) == [] and not gw.devices[DEVICE].restored


async def test_offers_older_than_a_restored_card_are_not_sent():
    service = Service()
    service.snapshot["cards"] = [snapshot_card("dlv_new", "tsk_a", "needs_input", gseq=9,
                                               interaction={"interaction_id": "int_q", "question": {"options": []}})]
    _, gw = gateway(service, url="http://agent:8010")
    assert await gw.restore(DEVICE)
    old = gw.offer(DEVICE, {"delivery_id": "dlv_old", "lease_id": LEASE, "gseq": 4, "attempt": 1, "kind": "agent",
                            "tier": "SILENT", "chat_id": "cht_1", "task_id": "tsk_a",
                            "payload": {"state": "progress", "summary": "Working"}})
    assert old["superseded"] and gw.devices[DEVICE].mirror["tsk_a"].delivery_id == "dlv_new"
    assert await wait_for(lambda: service.acks("dlv_old") == [("dlv_old", "dropped")], 1)
    newer = gw.offer(DEVICE, {"delivery_id": "dlv_next", "lease_id": LEASE, "gseq": 12, "attempt": 1, "kind": "agent",
                              "tier": "SILENT", "chat_id": "cht_1", "task_id": "tsk_a",
                              "payload": {"state": "progress", "summary": "Working"}})
    assert "superseded" not in newer and gw.devices[DEVICE].mirror["tsk_a"].delivery_id == "dlv_next"


async def test_the_stream_opens_only_after_the_restore(monkeypatch):
    service = Service()
    service.snapshot["chats"] = {"device_id": DEVICE, "current_chat_id": "cht_1", "items": [
        {"chat_id": "cht_1", "title": "Lamp", "title_source": "first_utterance", "state": "idle",
         "last_activity_at": "2026-10-11T10:00:00Z"}]}
    _, gw = gateway(service, url="http://agent:8010")
    opened = []

    class Http:
        def __init__(self, headers=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, params=None, timeout=None):
            opened.append((list(service.gets), gw.devices[DEVICE].restored))
            raise RuntimeError("stop")

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", Http)
    gw.on_poll(DEVICE, "1", "2")
    assert await wait_for(lambda: opened, 1)
    gets, restored = opened[0]
    assert gets == ["/v1/devices/7c%3A0c%3A5f%3A42%3A4b%3Afc/snapshot"] and restored
    assert gw.devices[DEVICE].chats.payload["current"] == "cht_1"
    for task in gw._sources.values():
        task.cancel()


# F13 INTERRUPT


class VoiceConn(Conn):
    def __init__(self):
        super().__init__()
        self.tts = object()
        self.client_is_speaking = True
        self.client_have_voice = False
        old = time.time() * 1000 - 10_000
        self.first_activity_time = old
        self.last_activity_time = time.time() * 1000
        self.dialogue = type("D", (), {"dialogue": [], "put": lambda self, m: self.dialogue.append(m)})()


def interrupt_stack(clock=None):
    stops = []

    async def interrupter(conn):
        stops.append(conn.session_id)
        conn.client_is_speaking = False

    calls = []

    async def speaker(conn, item, close_after):
        calls.append(item.id)
        return "spoken"

    clock = clock or time.monotonic
    proactive = wp.Proactive(TOKEN, clock=clock, auth=wp.DeviceAuth(True, [DEVICE]), interrupter=interrupter,
                             speaker=speaker)
    gw = wad.AgentGateway(proactive, clock=clock)
    proactive.agent = gw
    return proactive, gw, stops, calls


async def test_interrupt_item_stops_the_voice_turn_then_plays():
    proactive, gw, stops, calls = interrupt_stack()
    conn = VoiceConn()
    proactive.on_session_open(DEVICE, conn)
    urgent = gw.submit(DEVICE, card("t1", "unread", delivery="interrupt", speaks=True), speech="Leak!")
    msg = next(iter(proactive.messages.values()))
    assert msg.urgent and not proactive.quiescent(conn)
    proactive.dispatch_once()
    await settle()
    assert stops == ["s1"] and calls == []
    proactive.dispatch_once()
    await settle()
    assert calls == [msg.id] and urgent["speech_id"]


async def test_when_idle_items_and_the_users_voice_are_not_interrupted():
    proactive, gw, stops, calls = interrupt_stack()
    conn = VoiceConn()
    proactive.on_session_open(DEVICE, conn)
    gw.submit(DEVICE, card("t1", "unread", delivery="when_idle", speaks=True), speech="Later.")
    proactive.dispatch_once()
    await settle()
    assert stops == [] and calls == []
    proactive.queue.clear()
    gw.submit(DEVICE, card("t2", "unread", delivery="interrupt", speaks=True), speech="Now.")
    conn.client_have_voice = True
    proactive.dispatch_once()
    await settle()
    assert stops == [], "the user is speaking"
    conn.client_have_voice = False
    conn.first_activity_time = time.time() * 1000
    proactive.dispatch_once()
    await settle()
    assert stops == [], "a session that just opened settles first"


async def test_interrupt_cuts_a_playing_agent_announcement_and_accounts_for_it():
    proactive, gw, stops, calls = interrupt_stack()
    conn = VoiceConn()
    proactive.on_session_open(DEVICE, conn)
    first = gw.submit(DEVICE, card("t1", "unread", delivery="when_idle", speaks=True), speech="Report.")
    first_msg = next(m for m in proactive.messages.values())
    first_msg.agent["sentence_id"] = "s-a"
    conn._weixue_ack_sentence = "s-a"
    conn._weixue_busy = True
    conn._weixue_item = first_msg
    first_msg.status = "delivering"
    gw.submit(DEVICE, card("t2", "unread", delivery="interrupt", speaks=True), speech="Leak!")
    urgent_msg = next(m for m in proactive.messages.values() if m.urgent)
    proactive.dispatch_once()
    await settle()
    assert stops == ["s1"]
    assert gw.speeches[first["speech_id"]].state == "cut_off"
    cut = gw.sink.acks[-1]
    assert cut["outcome"] == "cut_off" and cut["reason"] == "superseded"
    proactive.dispatch_once()
    assert calls == [], "the cut delivery still holds the session"
    conn._weixue_busy = False
    conn._weixue_item = None
    proactive.dispatch_once()
    await settle()
    assert calls == [urgent_msg.id]
    gw.on_device_event(conn, wad.PLAYED_METHOD, {"v": 1, "boot": 1, "seq": 1, "sentence_id": "s-a", "complete": True})
    assert gw.sink.acks[-1]["outcome"] == "cut_off", "a late played ack of the cut sentence changes nothing"


async def test_an_urgent_delivery_is_never_interrupted():
    proactive, gw, stops, calls = interrupt_stack()
    conn = VoiceConn()
    proactive.on_session_open(DEVICE, conn)
    gw.submit(DEVICE, card("t1", "unread", delivery="interrupt", speaks=True), speech="First.")
    running = next(iter(proactive.messages.values()))
    proactive.queue.remove(running)
    conn._weixue_busy = True
    conn._weixue_item = running
    gw.submit(DEVICE, card("t2", "unread", delivery="interrupt", speaks=True), speech="Second.")
    proactive.dispatch_once()
    await settle()
    assert stops == [] and calls == []


async def test_a_slow_decision_is_not_sent_again_while_it_runs():
    service = Service(lambda path, body: (200, 0.2))
    _, gw = gateway(service)
    approval_card(gw)
    conn = Conn()
    gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1))
    assert await wait_for(lambda: receipts(conn), 1)
    assert receipts(conn)[-1]["result"] == "accepted" and len(service.posts) == 1


# Hung requests


async def test_a_hung_decision_is_stale_after_the_window():
    service = Service(lambda path, body: (200, HANG))
    _, gw = gateway(service)
    approval_card(gw)
    conn = Conn()
    start = time.monotonic()
    gw.on_device_event(conn, wad.RESPONSE_METHOD, approve(1))
    assert await wait_for(lambda: receipts(conn), 2)
    elapsed = time.monotonic() - start
    assert receipts(conn)[-1]["result"] == "stale"
    assert wad.DECISION_WINDOW_SECONDS <= elapsed < wad.DECISION_WINDOW_SECONDS + 0.5
    assert len(service.posts) == 1 and service.cancelled == 1, "one request, cut off by the caller's timeout"
    assert (DEVICE, "int_1") not in gw.card_decisions


async def test_a_hung_accepted_post_leaves_the_speech_unplayed():
    service = Service(lambda path, body: (200, HANG))
    proactive, gw = gateway(service)
    speech = gw.speeches[gw.offer(DEVICE, spoken_offer())["speech_id"]]
    start = time.monotonic()
    assert await wait_for(lambda: speech.state == "unaccepted", 2)
    assert time.monotonic() - start < wad.ACCEPT_WINDOW_SECONDS + 0.5
    assert service.cancelled == len(service.posts) >= 2, "each request bounded by REQUEST_SECONDS, then retried"
    assert proactive.messages == {} and not proactive.queue


async def test_a_hung_snapshot_get_is_bounded_and_retried_by_the_stream(monkeypatch):
    service = Service()
    service.snapshot_delay = HANG
    _, gw = gateway(service, url="http://agent:8010")
    start = time.monotonic()
    assert not await gw.restore(DEVICE)
    assert time.monotonic() - start < wad.REQUEST_SECONDS + 0.5 and service.cancelled == 1
    assert not gw.devices[DEVICE].restored and gw.poll_items(DEVICE) == []

    opened = []

    class Http:
        def __init__(self, headers=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, params=None, timeout=None):
            opened.append(len(service.gets))
            raise RuntimeError("stop")

    import aiohttp
    monkeypatch.setattr(aiohttp, "ClientSession", Http)
    stream = asyncio.get_running_loop().create_task(gw._stream(DEVICE))
    assert await wait_for(lambda: len(service.gets) == 2, 1)
    assert not opened, "no stream without the snapshot"
    service.snapshot_delay = 0.0
    assert await wait_for(lambda: opened, 3), "retried with the stream's backoff"
    assert gw.devices[DEVICE].restored and opened[0] == 3
    stream.cancel()
