"""Event turns: started when the chat is idle, queued behind a running turn."""
import asyncio
import uuid

from fake_hamcp import FakeHaMcp

PLUG = {"domain": "switch", "service": "turn_on", "entity_id": "switch.working_desk_plug"}


async def owned(h, row):
    h.fake_ha.elapse_wait()
    await h.wait_for(lambda: h.store.interaction(row["interaction_id"])["state"] == "pending", what="owned")


async def test_event_line_starts_an_event_turn_when_idle(make):
    prompts = []

    async def script(turn):
        prompts.append(turn.prompt)
        if turn.user_text.startswith("[event approval"):
            await turn.text("The plug is on.")
            return
        await turn.tool("ha_call_service", PLUG)
        await turn.text("Waiting for you.")
    h = await make(script, fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("plug on")
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    await owned(h, row)
    await h.http.post(f"/v1/chats/{first[0]['chat_id']}/approve",
                      json={"interaction_id": row["interaction_id"], "decision_id": str(uuid.uuid4()), "via": "panel"})
    task = await h.wait_task(task_id, "done")
    event_turn = h.store.one("SELECT * FROM turns WHERE task_id = ? AND kind = 'event'", (task_id,))
    assert event_turn is not None
    assert "executed. Already handled; do not call the tool again." in prompts[-1]
    done = h.events(first[0]["chat_id"], "done")[0]["data"]
    assert done["speak"] == "The plug is on." and done["tier"] == "WHEN_IDLE"


async def test_event_line_waits_for_the_running_turn(make):
    gate = asyncio.Event()

    async def script(turn):
        if turn.user_text.startswith("[event approval"):
            await turn.text("Noted the plug.")
            return
        if "other thing" in turn.prompt:
            await turn.text("Working on the other thing.")
            await turn.tool("ha_search", {"query": "x"})
            await gate.wait()
            await turn.text("Other thing done.")
            return
        await turn.tool("ha_call_service", PLUG)
        await turn.text("Waiting for you.")
    h = await make(script, fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("plug on")
    chat_id = first[0]["chat_id"]
    await h.wait_task(first[0]["task_id"], "needs_approval")
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (first[0]["task_id"],))
    await owned(h, row)
    _, cont = await h.send("and do the other thing")
    assert cont[0]["data"]["routed"] == "continue"
    await h.http.post(f"/v1/chats/{chat_id}/approve",
                      json={"interaction_id": row["interaction_id"], "decision_id": str(uuid.uuid4()), "via": "panel"})
    await h.wait_for(lambda: h.engine.chats.pending_notes(chat_id, "event"), what="note queued")
    assert h.engine.turn_in_flight(chat_id)
    assert not h.events(chat_id, "interrupted")
    gate.set()
    await h.idle(chat_id)
    assert any(p.split("\n")[1].startswith("[event approval") for p in h.factory.prompts[-1:])
