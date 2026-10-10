"""Steer (decision 36, brief section 9): a new message joins the running loop at the next tool boundary, or
cancels a reply still streaming after STEER_WAIT_S; barge-in cancels at once and discards the partial."""
import asyncio

from fake_api import blocks, last_user, system_notes

LIGHT = {"domain": "light", "service": "turn_on", "entity_id": "light.living_room_ceiling_lights"}


def boundary_script(seen):
    async def script(turn):
        await turn.text("On it.")
        await turn.tool("ha_search", {"query": "living room"})
        seen.append(turn.steer_text)
        if turn.steer_text:
            await turn.text("I've found the lights and I'm now looking at their history.")
        await turn.tool("ha_get_history", {"entity_id": "light.living_room_ceiling_lights"})
        await turn.text("Here's the draft automation.")
    return script


async def steer_during(h, coro_send, wait):
    task = asyncio.create_task(coro_send)
    await wait()
    return task


async def test_boundary_steer_appends_text_after_the_tool_results_and_a_separate_note(make):
    seen = []
    h = await make(boundary_script(seen))
    h.fake_ha.call_delay = 0.2
    first_task = asyncio.create_task(h.send("Look at the living room lights and draft an automation"))
    await h.wait_for(lambda: h.fake_ha.calls, what="first tool call")
    _, steer = await h.send("How is it going?")
    _, first = await first_task
    chat_id, task_id, turn_n = first[0]["chat_id"], first[0]["task_id"], first[0]["turn_id"]
    assert steer[0]["data"]["routed"] == "steer" and steer[0]["task_id"] == task_id
    assert "looking at their history" in "".join(e["data"]["delta"] for e in steer if e["type"] == "text")
    assert first[-1]["data"]["reason"] == "gateway_closed"
    interrupted = h.events(chat_id, "interrupted")[0]["data"]
    assert interrupted["turn_id"] == turn_n and interrupted["by_turn_id"] == steer[0]["turn_id"]
    assert interrupted["terminal_reason"] == "steered" and interrupted["forced"] is False
    assert h.api.cancelled == 0
    boundary = h.api.requests[1]
    user = last_user(boundary)
    kinds = [b["type"] for b in blocks(user)]
    assert kinds == ["tool_result", "text"] and blocks(user)[-1]["text"] == "How is it going?"
    assert boundary["messages"][-1]["role"] == "system"
    assert any('[note: the user spoke while you were working on "Look at the living room lights' in n
               for n in system_notes(boundary))
    await h.wait_task(task_id, "done")
    assert h.store.turn(turn_n)["state"] == "interrupted"
    assert h.store.turn(steer[0]["turn_id"])["state"] == "completed"
    assert seen == ["How is it going?"]


async def test_steer_while_streaming_waits_then_cancels_and_rewrites_the_tail(make):
    async def script(turn):
        if "How is it going?" in turn.user_text:
            await turn.text("Still working on it.")
            return
        await turn.text("Let me think about every light in the house in great detail", delay=0.1)
    h = await make(script)
    first_task = asyncio.create_task(h.send("Check every light"))
    await h.wait_for(lambda: h.events_of_type("text"), what="streaming")
    _, steer = await h.send("How is it going?")
    _, first = await first_task
    chat_id = first[0]["chat_id"]
    assert h.api.cancelled == 1
    interrupted = h.events(chat_id, "interrupted")[0]["data"]
    assert interrupted["terminal_reason"] == "steered" and interrupted["forced"] is True
    assert "Still working on it." in "".join(e["data"]["delta"] for e in steer if e["type"] == "text")
    retry = h.api.requests[-1]
    texts = [b["text"] for b in blocks(last_user(retry)) if b["type"] == "text"]
    assert texts == ["Check every light", "How is it going?"]
    rows = h.transcript(chat_id).rows()
    assert [r["role"] for r in rows] == ["user", "system", "assistant"]
    assert all("great detail" not in r["content_json"] for r in rows if r["role"] == "assistant")
    assert rows[0]["rewritten"] == 1


async def test_turn_that_finishes_before_the_boundary_becomes_a_continue_note(make):
    async def script(turn):
        if "How is it going?" in turn.user_text:
            await turn.text("It just finished: all lights are off.")
            return
        await turn.text("Checking.")
        await turn.tool("ha_search", {"query": "light"})
        await turn.text("All lights are off.", delay=0.05)
    h = await make(script)
    first_task = asyncio.create_task(h.send("Check all the lights"))
    await h.wait_for(lambda: len(h.api.requests) >= 2, what="second request")
    _, steer = await h.send("How is it going?")
    _, first = await first_task
    task_id = first[0]["task_id"]
    assert h.store.turn(first[0]["turn_id"])["state"] == "completed"
    assert not h.events(first[0]["chat_id"], "interrupted")
    notes = system_notes(h.api.requests[-1])
    assert any('[note: your work on "Check all the lights" finished just before this message; result: '
               '"All lights are off."' in n for n in notes)
    assert [d for d in h.deliveries(task_id=task_id, turn_id=first[0]["turn_id"]) if d["speak"]] == []
    await h.wait_task(task_id, "done")


async def test_second_steer_before_the_boundary_joins_the_first(make):
    seen = []
    h = await make(boundary_script(seen))
    h.fake_ha.call_delay = 0.25
    first_task = asyncio.create_task(h.send("Start the long thing"))
    await h.wait_for(lambda: h.fake_ha.calls, what="first tool call")
    one_task = asyncio.create_task(h.send("first change"))
    await h.wait_for(lambda: len(h.events_of_type("accepted")) >= 2, what="first steer accepted")
    _, two = await h.send("second change")
    _, one = await one_task
    await first_task
    assert one[-1]["data"]["reason"] == "gateway_closed"
    assert two[0]["turn_id"] == one[0]["turn_id"]
    assert seen[0] == "first change\nsecond change"


async def test_held_ask_user_returns_when_a_steer_arrives(make):
    results = []

    async def script(turn):
        if turn.user_text == "Remind me about the bins":
            results.append(await turn.ask("Which day?", ["Monday", "Thursday"]))
            results.append(turn.steer_text)
            await turn.text("Okay, I'll skip the reminder for now.")
            return
        await turn.text("Done.")
    h = await make(script)
    first_task = asyncio.create_task(h.send("Remind me about the bins"))
    await h.wait_for(lambda: h.events_of_type("needs_input"), what="question")
    device = h.store.device("sim-device-01")
    h.store.update("devices", "device_id", device["device_id"], {"voice_window_until": None})
    _, steer = await h.send("Actually never mind the bins")
    await first_task
    assert steer[0]["data"]["routed"] == "steer"
    assert results == ["No answer; the user's new message follows.", "Actually never mind the bins"]
    row = h.store.one("SELECT state FROM interactions")
    assert row["state"] == "cancelled"


async def test_in_flight_ha_call_is_reported_by_a_late_event_line_and_never_retried(make):
    async def script(turn):
        if turn.user_text.startswith("[event approval"):
            await turn.text("The light is on now.")
            return
        if "How is it going?" in turn.steer_text:
            await turn.text("Still waiting for Home Assistant.")
            return
        await turn.text("Turning it on.")
        result = await turn.tool("ha_call_service", LIGHT)
        if "How is it going?" in turn.steer_text:
            await turn.text("Still waiting for Home Assistant.")
            return
        await turn.text(result[:20])
    h = await make(script)
    h.fake_ha.call_delay = 0.8
    first_task = asyncio.create_task(h.send("Turn on the living room light"))
    await h.wait_for(lambda: h.fake_ha.calls, what="ha call sent")
    await h.send("How is it going?")
    _, first = await first_task
    chat_id = first[0]["chat_id"]
    await h.wait_for(lambda: h.store.one("SELECT * FROM approvals WHERE state = 'executed'"), what="late outcome")
    await h.wait_for(lambda: any("[event approval" in p and "executed" in p for p in h.api.prompts),
                     what="event line")
    await h.idle(chat_id)
    assert len(h.fake_ha.calls) == 1
    assert h.events(chat_id, "done")[-1]["data"]["speak"] == "The light is on now."


async def test_barge_in_cancels_the_stream_and_the_correction_rewrites_the_tail(make):
    async def script(turn):
        if "kitchen" in turn.user_text:
            await turn.text("The kitchen light is on.")
            return
        await turn.text("The living room light is off and the lamp by the window is also off and", delay=0.1)
    h = await make(script)
    first_task = asyncio.create_task(h.send("Is the living room light on?"))
    await h.wait_for(lambda: len(h.events_of_type("text")) >= 2, what="reply text")
    accepted = h.events_of_type("accepted")[0]
    closed_at = asyncio.get_running_loop().time()
    h.engine.gateway_closed(accepted["chat_id"], accepted["data"]["reply_id"])
    _, first = await first_task
    await h.wait_for(lambda: h.api.cancelled == 1, what="stream closed")
    assert h.api.cancel_times[0] - closed_at < 0.5
    assert first[-1]["data"]["reason"] == "gateway_closed"
    chat_id = first[0]["chat_id"]
    rows = h.transcript(chat_id).rows()
    assert [r["role"] for r in rows] == ["user", "system"] and all(r["state"] == "tail" for r in rows)
    _, correction = await h.send("I meant the kitchen")
    assert correction[0]["data"]["routed"] in ("steer", "continue")
    request = h.api.requests[-1]
    assert [b["text"] for b in blocks(last_user(request)) if b["type"] == "text"] == [
        "Is the living room light on?", "I meant the kitchen"]
    assert any(n.startswith("[note: your previous reply was cut off; the user heard: \"The")
               for n in system_notes(request))
    assert sum(1 for n in system_notes(request) if n.startswith("[context:")) == 1
    assert "kitchen light is on" in "".join(e["data"]["delta"] for e in correction if e["type"] == "text")
