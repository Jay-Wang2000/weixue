"""An open card stays the task's device item while turns run (finding 86): a steer or a continue message during a
pending hold keeps the needs_approval card, sends no working item over it and leaves the hold pending; an identical
held call from that turn joins the pending hold without a second card."""
import asyncio
import json
import uuid

from fake_hamcp import FakeHaMcp

PLUG = {"domain": "switch", "service": "turn_on", "entity_id": "switch.working_desk_plug"}
HELD_NOTE = "[note: a previous action is waiting for approval: switch.turn_on switch.working_desk_plug"


def hold_script(seen):
    async def script(turn):
        if turn.user_text.startswith("[event approval"):
            await turn.text("Okay, I didn't turn it on." if "denied" in turn.user_text else "It's on now.")
            return
        if turn.user_text == "plug on":
            await turn.text("Turning the plug on.")
            seen["first"] = await turn.tool("ha_call_service", PLUG)
            if turn.steer_text:
                seen["steer_notes"] = turn.notes
                seen["again"] = await turn.tool("ha_call_service", PLUG)
                await turn.tool("ha_get_state", {"entity_id": "sun.sun"})
                await turn.text("It's half past nine.")
                return
            await turn.text("Waiting for your OK.")
            return
        seen.setdefault("later_notes", []).append(turn.first_notes)
        await turn.tool("ha_get_state", {"entity_id": "sun.sun"})
        await turn.text("It's half past nine.")
    return script


def card_and_after(h, task_id):
    """(the task's first card delivery, every task-slot delivery queued after it)."""
    rows = h.deliveries(task_id=task_id)
    card = next(r for r in rows if r["slot"].startswith("int:"))
    later = [r for r in rows if r["gseq"] > card["gseq"] and r["slot"] == f"task:{task_id}"]
    return card, later


async def test_a_steer_during_a_pending_hold_keeps_the_card_and_the_hold(make):
    seen = {}
    h = await make(hold_script(seen), fake_ha=FakeHaMcp(wait_seconds=30))
    h.fake_ha.call_delay = 0.1
    first_task = asyncio.create_task(h.send("plug on", origin="api"))
    await h.wait_for(lambda: h.fake_ha.calls, what="held call sent")
    _, steer = await h.send("also tell me the time", origin="api")
    await first_task
    task_id, chat_id = steer[0]["task_id"], steer[0]["chat_id"]
    assert steer[0]["data"]["routed"] == "steer"
    await h.idle(chat_id)
    task = h.store.task(task_id)
    rows = h.store.all("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    assert task["state"] == "needs_approval"
    assert len(rows) == 1 and rows[0]["state"] in ("pending", "uncorrelated")
    assert seen["again"].startswith("PENDING_APPROVAL id=" + rows[0]["external_ref"])
    assert [c["tool"] for c in h.fake_ha.calls if c["tool"] == "ha_call_service"] == ["ha_call_service"]
    assert any(n.startswith(HELD_NOTE) for n in seen["steer_notes"])
    assert "It's half past nine." in "".join(e["data"]["delta"] for e in steer if e["type"] == "text")
    card, later = card_and_after(h, task_id)
    assert later == [], [json.loads(r["payload_json"])["state"] for r in later]
    assert h.store.delivery(card["delivery_id"])["state"] == "pending"
    assert not [r for r in h.store.all("SELECT * FROM task_transitions WHERE task_id = ?", (task_id,))
                if r["from_state"] == "needs_approval"]


async def test_a_message_to_a_task_waiting_on_its_card_keeps_the_card_until_the_deny(make):
    seen = {}
    h = await make(hold_script(seen), fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("plug on", origin="api")
    task_id, chat_id = first[0]["task_id"], first[0]["chat_id"]
    await h.wait_task(task_id, "needs_approval")
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    h.fake_ha.elapse_wait()
    await h.wait_for(lambda: h.store.interaction(row["interaction_id"])["state"] == "pending", what="owned hold")
    _, later_msg = await h.send("also tell me the time", origin="api")
    assert later_msg[0]["data"]["routed"] == "continue" and later_msg[0]["data"]["task_state"] == "needs_approval"
    await h.idle(chat_id)
    assert h.store.task(task_id)["state"] == "needs_approval"
    assert h.store.interaction(row["interaction_id"])["state"] == "pending"
    assert any(n.startswith(HELD_NOTE + " (on the device)") for n in seen["later_notes"][0])
    _, later = card_and_after(h, task_id)
    assert later == [], [json.loads(r["payload_json"])["state"] for r in later]
    assert h.store.one("SELECT COUNT(*) AS n FROM interactions WHERE task_id = ?", (task_id,))["n"] == 1
    resp = await h.http.post(f"/v1/chats/{chat_id}/deny", json={
        "interaction_id": row["interaction_id"], "decision_id": str(uuid.uuid4()), "via": "panel"})
    assert resp.status_code == 200, resp.text
    task = await h.wait_task(task_id, "done")
    assert task["outcome"] == "denied"
    assert "switch.working_desk_plug" not in [e["args"].get("entity_id") for e in h.fake_ha.executed]


async def test_a_steer_that_withdraws_a_held_question_puts_the_task_back_to_work(make):
    async def script(turn):
        if turn.user_text == "Remind me about the bins":
            await turn.ask("Which day?", ["Monday", "Thursday"])
            await turn.tool("ha_get_state", {"entity_id": "sun.sun"})
            await turn.text("Okay, I'll skip the reminder.")
            return
        await turn.text("Done.")
    h = await make(script)
    first_task = asyncio.create_task(h.send("Remind me about the bins"))
    await h.wait_for(lambda: h.events_of_type("needs_input"), what="question")
    h.store.update("devices", "device_id", "sim-device-01", {"voice_window_until": None})
    _, steer = await h.send("Actually never mind the bins")
    await first_task
    task_id = steer[0]["task_id"]
    await h.wait_task(task_id, "done")
    moves = [(r["from_state"], r["to_state"], r["reason"])
             for r in h.store.all("SELECT * FROM task_transitions WHERE task_id = ? ORDER BY id", (task_id,))]
    assert ("needs_input", "progress", "interaction closed") in moves
