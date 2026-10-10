"""Outbox tiers, leases, playback acks, no replay without evidence, steer supersession."""
import asyncio
import json

import pytest

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
