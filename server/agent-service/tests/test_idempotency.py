"""Idempotency-Key on messages and chats; decision_id on decisions."""
import uuid


async def test_same_key_and_body_is_a_duplicate_and_starts_nothing(make):
    h = await make()
    one = await h.send_json("hello", key="dev:sess:1")
    two = await h.send_json("hello", key="dev:sess:1")
    assert one["duplicate"] is False and two["duplicate"] is True
    assert one["turn_id"] == two["turn_id"]
    await h.idle(one["chat_id"])
    assert len(h.factory.prompts) == 1


async def test_duplicate_sse_replays_the_original_stream(make):
    h = await make()
    _, first = await h.send("hello", key="dev:sess:2")
    _, again = await h.send("hello", key="dev:sess:2")
    assert [e["seq"] for e in again] == [e["seq"] for e in first]


async def test_same_key_different_body_is_a_conflict(make):
    h = await make()
    await h.send_json("hello", key="dev:sess:3")
    resp = await h.http.post("/v1/chats/current/messages", json={"device_id": "sim-device-01", "text": "other"},
                             headers={"Idempotency-Key": "dev:sess:3"})
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "idempotency_mismatch"


async def test_missing_key_is_rejected(make):
    h = await make()
    resp = await h.http.post("/v1/chats/current/messages", json={"device_id": "sim-device-01", "text": "x"})
    assert resp.status_code == 400


async def test_decision_id_reused_for_another_decision_conflicts(make):
    h = await make()
    resp = await h.send_json("hello")
    decision = str(uuid.uuid4())
    one = await h.http.post(f"/v1/chats/{resp['chat_id']}/interrupt", json={"decision_id": decision, "reason": "api"})
    two = await h.http.post(f"/v1/chats/{resp['chat_id']}/interrupt", json={"decision_id": decision, "reason": "panel"})
    assert one.status_code == 200 and two.status_code == 409 and two.json()["error"]["code"] == "already_decided"


async def test_chat_create_is_idempotent(make):
    h = await make()
    one = await h.http.post("/v1/chats", json={"device_id": "sim-device-01"}, headers={"Idempotency-Key": "c1"})
    two = await h.http.post("/v1/chats", json={"device_id": "sim-device-01"}, headers={"Idempotency-Key": "c1"})
    assert one.status_code == 201 and two.json()["duplicate"] is True
    assert one.json()["chat_id"] == two.json()["chat_id"]
