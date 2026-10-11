"""Restart mid-turn: the task fails with service_restart, the next message continues the same transcript with
the open tool uses closed; speech without playback evidence is never repeated."""
import asyncio
import json
import uuid

from fake_api import blocks, last_user, system_notes
from weixue_agent import ha_adapter

LIGHT = {"domain": "light", "service": "turn_on", "entity_id": "light.living_room_ceiling_lights"}


async def test_restart_mid_turn_fails_with_service_restart_and_continues_the_transcript(make):
    gate = asyncio.Event()

    async def script(turn):
        if "restarted" in turn.prompt:
            await turn.text("Sorry, I was cut off; let me start again.")
            return
        await turn.text("Working on it.")
        await turn.tool("ha_search", {"query": "lights"})
        await gate.wait()
    h = await make(script)
    first_task = asyncio.create_task(h.send("Do the long task"))
    await h.wait_for(lambda: len(h.api.requests) >= 2, what="second request")
    accepted = h.events_of_type("accepted")[0]
    chat_id, task_id = accepted["chat_id"], accepted["task_id"]
    listing_before = (await h.http.get("/v1/chats", params={"device_id": "sim-device-01"})).json()
    await h.restart()
    first_task.cancel()
    task = h.store.task(task_id)
    assert task["state"] == "failed" and json.loads(task["error_json"])["code"] == "service_restart"
    assert task["outcome"] == "none" and task["retry_allowed"] == 1
    notice = [d for d in h.deliveries(task_id=task_id) if d["speak"]][-1]
    assert notice["tier"] == "WHEN_IDLE"
    assert notice["speak"] == "I restarted while working on Do the long task, so it may not have finished."
    assert json.loads(notice["payload_json"])["options"] == [{"id": "retry", "label": "Retry"}]
    listing_after = (await h.http.get("/v1/chats", params={"device_id": "sim-device-01"})).json()
    assert [c["chat_id"] for c in listing_after["items"]] == [c["chat_id"] for c in listing_before["items"]]
    assert listing_after["current_chat_id"] == listing_before["current_chat_id"]
    _, again = await h.send("are you there?")
    request = h.api.requests[-1]
    assert request["messages"][0]["content"][0]["text"] == "Do the long task"
    user = last_user(request)
    assert [b["type"] for b in blocks(user)] == ["tool_result", "text"]
    assert any('[note: the service restarted while you were working on "Do the long task"' in n
               for n in system_notes(request))
    assert "start again" in "".join(e["data"]["delta"] for e in again if e["type"] == "text")


async def test_restart_with_an_unknown_ha_call_offers_check_state_not_retry(make, monkeypatch):
    monkeypatch.setattr(ha_adapter, "HA_CALL_BUDGET_S", 5)

    async def script(turn):
        if "check" in turn.user_text:
            await turn.text("Let me look.")
            return
        await turn.text("Turning it on.")
        await turn.tool("ha_call_service", LIGHT)
    h = await make(script)
    h.fake_ha.call_delay = 5
    first_task = asyncio.create_task(h.send("Turn on the light"))
    await h.wait_for(lambda: h.fake_ha.calls, what="sent")
    accepted = h.events_of_type("accepted")[0]
    await h.restart()
    first_task.cancel()
    task = h.store.task(accepted["task_id"])
    assert task["outcome"] == "unknown" and task["retry_allowed"] == 0
    notice = [d for d in h.deliveries(task_id=task["task_id"]) if d["speak"]][-1]
    assert json.loads(notice["payload_json"])["options"] == [{"id": "check", "label": "Check state"}]
    retry = await h.http.post("/v1/chats/current/messages",
                              json={"device_id": "sim-device-01", "text": "retry", "retry_of": task["task_id"]},
                              headers={"Idempotency-Key": str(uuid.uuid4())})
    assert retry.status_code == 409
    assert "these calls have an unknown result, do not repeat them: ha_call_service" in \
        h.engine.chats.pending_notes(accepted["chat_id"])[0]["text"]
    await h.send("check it")
    results = [b for b in blocks(last_user(h.api.requests[-1])) if b["type"] == "tool_result"]
    assert results and results[0]["content"] == "result unknown: the service restarted; do not repeat it"


async def test_restart_never_repeats_speech_that_may_have_started(make):
    gate = asyncio.Event()

    async def script(turn):
        await turn.text("On it.")
        await turn.tool("ha_search", {"query": "x"})
        await gate.wait()
        await turn.text("Here is the result.")
    h = await make(script)
    _, first = await h.send("long")
    assert first[-1]["data"]["reason"] == "hold_elapsed"
    gate.set()
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "done")
    row = [d for d in h.deliveries(task_id=task_id) if d["speak"]][-1]
    offer = h.service.outbox.offer(row)
    h.service.outbox.ack(row["delivery_id"], {"lease_id": offer["lease_id"], "outcome": "accepted",
                                              "proactive_id": "p1", "ttl_s": 600})
    await h.restart()
    row = h.store.delivery(row["delivery_id"])
    assert row["state"] == "uncertain" and row["playback"] == "uncertain"
    offers = h.service.outbox.next_offers("sim-device-01")
    assert all(o["speak"] is None for o in offers)
    card = json.loads(h.deliveries(slot=f"task:{task_id}")[-1]["payload_json"])
    assert card["playback"] == "uncertain" and card["unread"] is True


def test_a_v1_database_migrates_to_v2(tmp_path):
    import sqlite3
    from weixue_agent import store as st
    v1 = st._SCHEMA.replace("  token_source TEXT,\n", "").replace(",\n  dispatch_id TEXT\n", "\n")
    assert "token_source" not in v1 and "dispatch_id" not in v1
    path = tmp_path / "v1.db"
    raw = sqlite3.connect(path)
    raw.executescript("BEGIN;" + v1 + "PRAGMA user_version=1;COMMIT;")
    raw.execute("INSERT INTO approvals (approval_id, tool, args_json, args_hash, state, ha_token, created_at, "
                "updated_at) VALUES ('apr_old', 'ha_call_service', '{}', 'h', 'pending', 'listed-token', 'x', 'x')")
    raw.commit()
    raw.close()
    store = st.Store(path)
    assert store.scalar("PRAGMA user_version") == st.SCHEMA_VERSION == 2
    row = store.approval("apr_old")
    assert row["token_source"] is None and not ha_adapter.HaGateway.owned(row)
    assert "dispatch_id" in [c["name"] for c in store.all("PRAGMA table_info(tool_calls)")]
    store.close()
