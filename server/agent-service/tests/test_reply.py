"""reply_end reasons, hold, canned acks; nothing streamed after reply_end."""
import asyncio

from conftest import DEVICE


async def test_short_turn_ends_with_turn_done_and_done_speak_null(make):
    async def script(turn):
        await turn.text("The light is off.")
    h = await make(script)
    resp, events = await h.send("Is the living room ceiling light on?")
    types = [e["type"] for e in events]
    assert types[0] == "accepted" and types[-1] == "reply_end"
    assert events[0]["data"]["routed"] == "new_task"
    assert events[-1]["data"]["reason"] == "turn_done"
    assert "".join(e["data"]["delta"] for e in events if e["type"] == "text").strip() == "The light is off."
    chat_id = events[0]["chat_id"]
    task_id = events[0]["task_id"]
    task = await h.wait_task(task_id, "done")
    done = h.events(chat_id, "done")[0]["data"]
    assert done["speak"] is None and done["outcome"] == "ok"
    assert resp.headers["x-turn-id"] == events[0]["turn_id"]
    cards = h.deliveries(task_id=task_id, slot=f"task:{task_id}")
    assert cards[-1]["tier"] == "SILENT" and cards[-1]["speak"] is None
    assert task["summary"] == "The light is off."


async def test_hold_elapsed_after_text_then_tool_and_result_delivered_when_idle(make):
    gate = asyncio.Event()

    async def script(turn):
        await turn.text("On it, checking the lights.")
        await turn.tool("ha_get_state", {"entity_id": "light.living_room_ceiling_lights"})
        await gate.wait()
        await turn.text("All done: the light is off.")
    h = await make(script)
    _, events = await h.send("Check the lights and tell me later")
    assert events[-1]["type"] == "reply_end" and events[-1]["data"]["reason"] == "hold_elapsed"
    spoken = "".join(e["data"]["delta"] for e in events if e["type"] == "text")
    assert "On it" in spoken and "All done" not in spoken
    gate.set()
    task = await h.wait_task(events[0]["task_id"], "done")
    done = h.events(events[0]["chat_id"], "done")[0]["data"]
    assert done["speak"] == "All done: the light is off." and done["tier"] == "WHEN_IDLE"
    spoken_rows = [d for d in h.deliveries(task_id=task["task_id"]) if d["speak"]]
    assert spoken_rows[-1]["tier"] == "WHEN_IDLE"
    assert "All done" not in "".join(e["data"]["delta"] for e in h.events(events[0]["chat_id"], "text"))


async def test_canned_ack_when_no_text_within_ack_after(make):
    gate = asyncio.Event()

    async def script(turn):
        await turn.tool("ha_search", {"query": "lights"})
        await gate.wait()
        await turn.text("Found them.")
    h = await make(script)
    _, events = await h.send("Find all the lights")
    assert events[-1]["data"]["reason"] == "ack"
    assert events[-1]["data"]["ack_text"] == "On it."
    gate.set()
    await h.wait_task(events[0]["task_id"], "done")


async def test_barge_in_without_a_follow_up_ends_a_quick_reply(make):
    async def script(turn):
        await turn.text("Let me think about this for a while and tell you all about it.", delay=0.1)
    h = await make(script)
    response = await h.send_json("Think about it")
    chat_id = response["chat_id"]
    await h.wait_for(lambda: h.events(chat_id, "text"), what="text")
    reply_id = h.events(chat_id, "accepted")[0]["data"]["reply_id"]
    h.engine.gateway_closed(chat_id, reply_id)
    assert h.events(chat_id, "reply_end")[0]["data"]["reason"] == "gateway_closed"
    task = await h.wait_task(response["task_id"], "cancelled")
    assert task["outcome"] == "none"
    assert h.events(chat_id, "interrupted")[0]["data"]["terminal_reason"] == "barge_in"
    await h.send("next")
    assert any("[note: your previous reply was cut off; the user heard:" in p and p.endswith("next")
               for p in h.api.prompts)


async def test_barge_in_during_task_work_resumes_the_task(make):
    async def script(turn):
        if turn.model == "claude-fable-5-1" and not turn.user_text.startswith("Plan"):
            await turn.text("Back to it: the plan is ready.")
            return
        await turn.text("On it.")
        await turn.tool("start_task", {"title": "Plan the week"})
        await turn.text("Here is a long explanation of the plan that keeps going and going.", delay=0.1)
    h = await make(script)
    response = await h.send_json("Plan my week")
    chat_id = response["chat_id"]
    await h.wait_for(lambda: len(h.api.requests) >= 2 and h.events(chat_id, "text"), what="task text")
    await h.wait_for(lambda: "".join(e["data"]["delta"] for e in h.events(chat_id, "text")).count(" ") > 3,
                     what="more text")
    reply_id = h.events(chat_id, "accepted")[0]["data"]["reply_id"]
    h.engine.gateway_closed(chat_id, reply_id)
    task = await h.wait_task(response["task_id"], "done")
    assert h.store.one("SELECT kind FROM turns WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                       (task["task_id"],))["kind"] == "event"
    assert h.events(chat_id, "done")[-1]["data"]["speak"] == "Back to it: the plan is ready."


async def test_a_spoken_question_at_the_end_of_an_answer_is_speech_and_the_task_ends_done(make):
    async def script(turn):
        if "Thursday" in turn.prompt:
            await turn.text("Okay, I'll remind you on Thursday.")
            return
        await turn.text("Sure. Which day are the bins?")
    h = await make(script)
    _, events = await h.send("Remind me about the bins")
    assert events[-1]["data"]["reason"] == "turn_done"
    task_id = events[0]["task_id"]
    await h.wait_task(task_id, "done")
    assert h.store.scalar("SELECT COUNT(*) FROM interactions") == 0
    assert not h.events_of_type("needs_input")
    _, events2 = await h.send("Thursday")
    assert events2[0]["data"]["routed"] == "new_task" and events2[0]["chat_id"] == events[0]["chat_id"]
    await h.wait_task(events2[0]["task_id"], "done")


async def test_context_line_prefixes_user_text(make):
    h = await make()
    await h.send("hello")
    prompt = h.factory.prompts[-1]
    assert prompt.startswith("[context: local time ")
    assert f"device {DEVICE}" in prompt and "reply language en" in prompt
    assert prompt.endswith("hello")


async def test_a_turn_that_only_wrote_a_progress_note_speaks_it(make):
    async def script(turn):
        turn.thinking("I've checked the lights; the history is next.")
        await turn.tool("ha_search", {"query": "lights"})
    h = await make(script)
    _, events = await h.send("How is it going?")
    assert "".join(e["data"]["delta"] for e in events if e["type"] == "text") == \
        "I've checked the lights; the history is next."
    assert events[-1]["data"]["reason"] == "turn_done"


async def test_a_progress_note_that_asks_is_spoken_and_the_task_ends_done(make):
    async def script(turn):
        turn.thinking("The draft is ready. What would you like me to do next?")
        await turn.tool("ha_search", {"query": "lights"})
    h = await make(script)
    _, events = await h.send("How is it going?")
    assert events[-1]["data"]["reason"] == "turn_done"
    await h.wait_task(events[0]["task_id"], "done")
    assert not h.events_of_type("needs_input")
