"""Outbox tiers, leases, playback acks, no replay without evidence, steer supersession, closed cards."""
import asyncio
import json
import uuid

import pytest

from fake_hamcp import FakeHaMcp
from weixue_agent.deliveries import StaleLease

DEVICE = "sim-device-01"


async def test_offers_are_ordered_by_tier_then_sequence(make):
    h = await make()
    box = h.service.outbox
    silent = box.enqueue(DEVICE, "agent", "SILENT", "a", {"task_id": "t"})
    idle = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "b", {"task_id": "t"}, speak="hi")
    urgent = box.enqueue(DEVICE, "notify", "INTERRUPT", "c", {}, speak="now")
    assert [r["delivery_id"] for r in box.next_offers(DEVICE)] == [urgent, idle, silent]


async def test_two_phase_ack_played_and_cut_off(make):
    h = await make()
    box = h.service.outbox
    did = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "task:t1", {"task_id": "t1", "chat_id": "c"}, task_id="t1", speak="x")
    offer = box.offer(h.store.delivery(did))
    assert offer["idempotency_key"] == f"{did}:1" and offer["urgent"] is False
    with pytest.raises(StaleLease):
        box.ack(did, {"lease_id": "00000000-0000-0000-0000-000000000000", "outcome": "accepted"})
    box.ack(did, {"lease_id": offer["lease_id"], "outcome": "accepted", "proactive_id": "p", "ttl_s": 60})
    box.ack(did, {"lease_id": offer["lease_id"], "outcome": "spoken"})
    box.ack(did, {"lease_id": offer["lease_id"], "outcome": "cut_off", "sentence_id": "s1", "cut_at_ms": 1840,
                  "reason": "wake", "boot": 7, "device_seq": 3})
    row = h.store.delivery(did)
    assert row["state"] == "delivered" and row["playback"] == "cut_off"
    acks = h.store.all("SELECT * FROM delivery_acks WHERE delivery_id = ? ORDER BY id", (did,))
    assert [a["outcome"] for a in acks] == ["accepted", "spoken", "cut_off"]
    assert acks[-1]["cut_at_ms"] == 1840 and acks[-1]["sentence_id"] == "s1" and acks[-1]["boot"] == 7
    card = json.loads(h.deliveries(slot="task:t1")[-1]["payload_json"])
    assert card["playback"] == "cut_off" and card["unread"] is True


async def test_deferred_returns_to_pending_with_a_new_key(make):
    h = await make()
    box = h.service.outbox
    did = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "s", {}, speak="x")
    offer = box.offer(h.store.delivery(did))
    box.ack(did, {"lease_id": offer["lease_id"], "outcome": "deferred", "retry_after_ms": 0})
    again = box.offer(h.store.delivery(did))
    assert again["idempotency_key"] == f"{did}:2"


async def test_handed_off_past_lease_is_uncertain_and_lost_is_never_replayed(make):
    h = await make()
    box = h.service.outbox
    did = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "task:t2", {"task_id": "t2"}, task_id="t2", speak="x")
    offer = box.offer(h.store.delivery(did))
    box.ack(did, {"lease_id": offer["lease_id"], "outcome": "accepted", "ttl_s": 10})
    h.store.update("deliveries", "delivery_id", did, {"lease_until": "2000-01-01T00:00:00.000Z"})
    box.sweep()
    assert h.store.delivery(did)["state"] == "uncertain"
    other = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "task:t3", {"task_id": "t3"}, task_id="t3", speak="y")
    offer = box.offer(h.store.delivery(other))
    box.ack(other, {"lease_id": offer["lease_id"], "outcome": "lost"})
    assert h.store.delivery(other)["state"] == "uncertain"
    assert all(r["speak"] is None for r in box.next_offers(DEVICE))


async def test_steer_supersedes_a_pending_spoken_result(make):
    gate = asyncio.Event()

    async def script(turn):
        if "and also" in turn.prompt:
            await turn.text("Sure.")
            return
        await turn.text("Working.")
        await turn.tool("ha_search", {"query": "x"})
        await gate.wait()
        await turn.text("Result.")
    h = await make(script)
    _, first = await h.send("long task")
    task_id = first[0]["task_id"]
    gate.set()
    await h.wait_task(task_id, "done")
    spoken = [d for d in h.deliveries(task_id=task_id) if d["speak"]]
    assert spoken[-1]["state"] == "pending"
    h.store.update("tasks", "task_id", task_id, {"state": "progress"})
    h.store.update("chats", "chat_id", first[0]["chat_id"], {"open_task_id": task_id})
    await h.send("and also this")
    assert h.store.delivery(spoken[-1]["delivery_id"])["state"] == "superseded"


async def test_ack_route_records_user_ack_and_marks_the_chat_read(make):
    h = await make()
    first = await h.send_json("hi")
    await h.idle(first["chat_id"])
    row = h.deliveries(slot=f"task:{first['task_id']}")[-1]
    offer = h.service.outbox.offer(row)
    resp = await h.http.post(f"/v1/deliveries/{row['delivery_id']}/ack",
                             json={"lease_id": offer["lease_id"], "outcome": "shown"})
    assert resp.status_code == 200
    resp = await h.http.post(f"/v1/deliveries/{row['delivery_id']}/ack",
                             json={"lease_id": offer["lease_id"], "outcome": "user_ack"})
    assert resp.status_code == 200
    assert (await h.http.get(f"/v1/chats/{first['chat_id']}")).json()["unread"] is False
    stale = await h.http.post(f"/v1/deliveries/{row['delivery_id']}/ack",
                              json={"lease_id": "00000000-0000-0000-0000-000000000000", "outcome": "shown"})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_lease"


async def test_snapshot_serves_the_latest_card_per_slot(make):
    h = await make()
    first = await h.send_json("hi")
    await h.idle(first["chat_id"])
    snap = (await h.http.get(f"/v1/devices/{DEVICE}/snapshot")).json()
    slots = [c["slot"] for c in snap["cards"]]
    assert f"task:{first['task_id']}" in slots and len(slots) == len(set(slots))
    assert all(c["speak"] is None for c in snap["cards"])
    assert snap["chats"]["current_chat_id"] == first["chat_id"]


async def test_connection_events_are_recorded(make):
    h = await make()
    resp = await h.http.post(f"/v1/devices/{DEVICE}/connection", json={"kind": "open", "gateway_session_id": "g1"})
    assert resp.status_code == 200
    await h.http.post(f"/v1/devices/{DEVICE}/connection", json={"kind": "close"})
    kinds = [r["kind"] for r in h.store.all("SELECT kind FROM connection_events WHERE device_id = ?", (DEVICE,))]
    assert kinds == ["open", "close"]
    assert h.store.device(DEVICE)["connected"] == 0


async def test_acks_never_move_a_delivery_backwards(make):
    """F10: played then a late accepted keeps delivered; terminal acks are idempotent."""
    h = await make()
    box = h.service.outbox
    did = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "task:m1", {"task_id": "m1"}, task_id="m1", speak="x")
    offer = box.offer(h.store.delivery(did))
    lease = offer["lease_id"]
    box.ack(did, {"lease_id": lease, "outcome": "accepted", "ttl_s": 60})
    box.ack(did, {"lease_id": lease, "outcome": "played"})
    with pytest.raises(StaleLease):
        box.ack(did, {"lease_id": lease, "outcome": "accepted", "ttl_s": 60})
    for outcome in ("played", "cut_off", "lost", "spoken", "dropped", "shown"):
        box.ack(did, {"lease_id": lease, "outcome": outcome})
    with pytest.raises(StaleLease):
        box.ack(did, {"lease_id": lease, "outcome": "deferred"})
    row = h.store.delivery(did)
    assert row["state"] == "delivered" and row["playback"] == "played"
    ignored = h.store.all("SELECT detail FROM delivery_acks WHERE delivery_id = ? AND detail LIKE 'ignored%'", (did,))
    assert len(ignored) == 6


async def test_a_late_played_ack_upgrades_uncertain_playback(make):
    h = await make()
    box = h.service.outbox
    did = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "task:m2", {"task_id": "m2"}, task_id="m2", speak="x")
    offer = box.offer(h.store.delivery(did))
    box.ack(did, {"lease_id": offer["lease_id"], "outcome": "accepted", "ttl_s": 10})
    h.store.update("deliveries", "delivery_id", did, {"lease_until": "2000-01-01T00:00:00.000Z"})
    box.sweep()
    assert h.store.delivery(did)["state"] == "uncertain"
    box.ack(did, {"lease_id": offer["lease_id"], "outcome": "played"})
    assert h.store.delivery(did)["playback"] == "played"
    card = json.loads(h.deliveries(slot="task:m2")[-1]["payload_json"])
    assert card["playback"] == "played"


async def test_the_service_owns_retries_and_acks_carry_the_offer_attempt(make):
    h = await make()
    box = h.service.outbox
    did = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "task:m3", {"task_id": "m3"}, task_id="m3", speak="x")
    first = box.offer(h.store.delivery(did))
    box.ack(did, {"lease_id": first["lease_id"], "outcome": "deferred", "attempt": 1})
    with pytest.raises(StaleLease):
        box.ack(did, {"lease_id": first["lease_id"], "outcome": "accepted", "attempt": 1})
    second = box.offer(h.store.delivery(did))
    assert second["attempt"] == 2 and second["lease_id"] != first["lease_id"]
    with pytest.raises(StaleLease):
        box.ack(did, {"lease_id": second["lease_id"], "outcome": "accepted", "attempt": 1})
    box.ack(did, {"lease_id": second["lease_id"], "outcome": "accepted", "attempt": 2, "ttl_s": 60})
    assert h.store.delivery(did)["state"] == "handed_off"


async def test_a_revoked_consumer_cannot_hand_off_its_old_offer(make):
    h = await make()
    box = h.service.outbox
    did = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "task:m4", {"task_id": "m4"}, task_id="m4", speak="x")
    box.register_consumer(DEVICE)
    old = box.offer(h.store.delivery(did))
    box.register_consumer(DEVICE)
    with pytest.raises(StaleLease):
        box.ack(did, {"lease_id": old["lease_id"], "outcome": "accepted", "ttl_s": 60})
    assert h.store.delivery(did)["state"] == "pending"


async def test_restart_never_reoffers_offered_speech(make):
    """F09: a missing acceptance ack is no evidence that speech did not start."""
    h = await make()
    box = h.service.outbox
    spoken = box.enqueue(DEVICE, "agent", "WHEN_IDLE", "task:m5", {"task_id": "m5"}, task_id="m5", speak="x")
    card = box.enqueue(DEVICE, "agent", "SILENT", "task:m6", {"task_id": "m6"}, task_id="m6")
    box.offer(h.store.delivery(spoken))
    box.offer(h.store.delivery(card))
    await h.restart()
    assert h.store.delivery(spoken)["state"] == "uncertain"
    assert h.store.delivery(card)["state"] == "pending" and h.store.delivery(card)["lease_id"] is None
    assert all(o["speak"] is None for o in h.service.outbox.next_offers(DEVICE))


async def test_notify_user_is_an_unread_agent_card_spoken_at_its_tier(make):
    """F12: notices use the agent kind the gateway carries, with their own card id, tier, TTL and acks."""
    async def script(turn):
        await turn.tool("notify_user", {"text": "The washing machine finished.", "urgent": True})
        await turn.tool("notify_user", {"text": "Bins go out tonight."})
        await turn.text("Told you.")
    h = await make(script)
    _, events = await h.send("remind me")
    await h.wait_task(events[0]["task_id"], "done")
    notices = [d for d in h.deliveries() if d["slot"].startswith("notify:")]
    assert [(d["kind"], d["tier"]) for d in notices] == [("agent", "INTERRUPT"), ("agent", "WHEN_IDLE")]
    payload = json.loads(notices[0]["payload_json"])
    assert payload["state"] == "unread" and payload["task_id"] == notices[0]["task_id"]
    assert payload["task_id"].startswith("ntf_") and len(payload["task_id"]) <= 36
    assert payload["origin_task_id"] == events[0]["task_id"] and payload["speaks"] is True
    assert notices[0]["speak"] == "The washing machine finished." and notices[0]["expires_at"]
    offer = h.service.outbox.offer(notices[0])
    assert offer["urgent"] is True and offer["kind"] == "agent"
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2] / "overlay"))
    import weixue_agent_msg as wire
    fields, speech = wire.from_delivery(offer)
    assert fields["state"] == "unread" and fields["delivery"] == "interrupt" and speech == notices[0]["speak"]
    item = wire.build_item(fields, 1, 0)
    assert item["task_id"] == payload["task_id"] and item["state"] == "unread" and item["delivery"] == "interrupt"
    trace = (await h.http.get(f"/v1/tasks/{events[0]['task_id']}/trace")).json()
    assert len([d for d in trace["deliveries"] if d["slot"].startswith("notify:")]) == 2


async def _approval_card_then_cancel(make):
    """An owned approval card handed off for speech, then its task cancelled from the panel."""
    async def script(turn):
        await turn.tool("ha_call_service", {"domain": "switch", "service": "turn_on",
                                            "entity_id": "switch.working_desk_plug"})
        await turn.text("Needs your OK.")
    h = await make(script, fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("plug on", origin="api")
    task_id, chat_id = first[0]["task_id"], first[0]["chat_id"]
    await h.wait_task(task_id, "needs_approval")
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    h.fake_ha.elapse_wait()
    await h.wait_for(lambda: h.store.interaction(row["interaction_id"])["state"] == "pending", what="owned")
    slot = f"int:{row['interaction_id']}"
    spoken = await h.wait_for(lambda: [d for d in h.deliveries(slot=slot) if d["speak"] and d["state"] == "pending"],
                              what="spoken card")
    box = h.service.outbox
    offer = box.offer(spoken[-1])
    box.ack(offer["delivery_id"], {"lease_id": offer["lease_id"], "outcome": "accepted", "ttl_s": 60})
    resp = await h.http.post(f"/v1/chats/{chat_id}/interrupt",
                             json={"decision_id": str(uuid.uuid4()), "reason": "panel"})
    assert resp.json()["task"]["state"] == "cancelled"
    return h, task_id, slot, offer


async def test_a_lost_ack_after_the_task_ended_queues_no_open_card(make):
    h, task_id, slot, offer = await _approval_card_then_cancel(make)
    before = len(h.deliveries(slot=slot))
    h.service.outbox.ack(offer["delivery_id"], {"lease_id": offer["lease_id"], "outcome": "lost"})
    assert h.store.delivery(offer["delivery_id"])["playback"] == "uncertain"
    assert len(h.deliveries(slot=slot)) == before, "no needs_approval card re-queued for a cancelled task"
    snap = (await h.http.get(f"/v1/devices/{DEVICE}/snapshot")).json()
    states = [c["payload"]["state"] for c in snap["cards"] if c["task_id"] == task_id]
    assert states == ["cancelled"]


async def test_the_snapshot_leaves_out_an_open_card_of_an_ended_task(make):
    h, task_id, slot, offer = await _approval_card_then_cancel(make)
    card = json.loads(h.store.delivery(offer["delivery_id"])["payload_json"])
    h.service.outbox.enqueue(DEVICE, "agent", "SILENT", slot, dict(card, playback="uncertain"), task_id=task_id,
                             chat_id=card["chat_id"])
    snap = (await h.http.get(f"/v1/devices/{DEVICE}/snapshot")).json()
    states = [c["payload"]["state"] for c in snap["cards"] if c["task_id"] == task_id]
    assert states == ["cancelled"]
