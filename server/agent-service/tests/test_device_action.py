"""device_action app tool: SSE event on the reply stream, result route, whitelist, closed reply, timeout;
the sentence before a tool call reaches the stream first; the reply's first clause is not coalesced."""
import asyncio
import time

from weixue_agent import turns


async def _post_result(h, chat_id, action_id, body):
    return await h.http.post(f"/v1/chats/{chat_id}/device_actions/{action_id}", json=body)


async def _next_action(h):
    return await h.wait_for(lambda: (h.events_of_type("device_action") or [None])[-1], what="device_action event")


async def test_device_action_is_an_event_of_the_reply_stream_and_its_result_reaches_the_model(make):
    results = []

    async def script(turn):
        await turn.text("Switching to bedside mode.")
        results.append(await turn.tool("device_action", {"tool": "self.screen.set_bedside", "args": {"mode": "night"}}))
        await turn.text("Done.")
    h = await make(script)
    send = asyncio.create_task(h.send("Set bedside mode"))
    event = await _next_action(h)
    assert event["data"]["tool"] == "self.screen.set_bedside" and event["data"]["args"] == {"mode": "night"}
    resp = await _post_result(h, event["chat_id"], event["data"]["action_id"], {"ok": True, "result": '{"night":true}'})
    assert resp.status_code == 200 and resp.json()["accepted"] is True
    _, events = await send
    types = [e["type"] for e in events]
    assert "device_action" in types and types.index("device_action") < types.index("reply_end")
    assert events[-1]["data"]["reason"] == "turn_done"
    assert results == ['Device result: {"night":true}']
    again = await _post_result(h, event["chat_id"], event["data"]["action_id"], {"ok": True})
    assert again.status_code == 404


async def test_device_error_is_returned_to_the_model(make):
    results = []

    async def script(turn):
        results.append(await turn.tool("device_action", {"tool": "self.audio_speaker.set_volume",
                                                         "args": {"volume": 40}}))
        await turn.text("I couldn't change it.")
    h = await make(script)
    send = asyncio.create_task(h.send("Volume forty"))
    event = await _next_action(h)
    resp = await _post_result(h, event["chat_id"], event["data"]["action_id"], {"ok": False, "error": "device MCP not ready"})
    assert resp.status_code == 200
    await send
    assert results == ["The device reported an error: device MCP not ready"]


async def test_tools_outside_the_agent_visible_list_never_reach_the_stream(make):
    results = []

    async def script(turn):
        for tool in ("self.diag.gesture", "self.system.reconfigure_wifi", "self.camera.take_photo"):
            results.append(await turn.tool("device_action", {"tool": tool, "args": {}}))
        await turn.text("I can't do that.")
    h = await make(script)
    _, events = await h.send("Pick yourself up")
    assert all(r.startswith("Unknown device tool") for r in results) and len(results) == 3
    assert "device_action" not in [e["type"] for e in events]
    assert h.events_of_type("device_action") == []


async def test_device_action_after_the_reply_ended_is_refused(make):
    results = []

    async def script(turn):
        await turn.text("On it.")
        await turn.tool("start_task", {"title": "Bedside"})
        await asyncio.sleep(0.7)
        results.append(await turn.tool("device_action", {"tool": "self.screen.set_bedside", "args": {"mode": "night"}}))
        await turn.text("Done.")
    h = await make(script)
    _, events = await h.send("Later set bedside mode")
    assert events[-1]["data"]["reason"] == "hold_elapsed"
    await h.wait_task(events[0]["task_id"], "done")
    assert results and results[0].startswith("The device is not in a conversation right now")
    assert h.events_of_type("device_action") == []


async def test_device_action_without_a_result_times_out_as_unknown(make, monkeypatch):
    monkeypatch.setattr(turns, "DEVICE_ACTION_WAIT_S", 0.3)
    results = []

    async def script(turn):
        results.append(await turn.tool("device_action", {"tool": "self.screen.set_theme", "args": {"theme": "dark"}}))
        await turn.text("It may not have changed.")
    h = await make(script)
    _, events = await h.send("Dark theme please")
    assert "device_action" in [e["type"] for e in events]
    assert results == ["The device did not answer in time; the outcome is unknown. Do not repeat it."]
    assert h.engine.device_actions == {}


async def test_first_text_delta_of_a_reply_is_not_coalesced(make, monkeypatch):
    monkeypatch.setattr(turns, "TEXT_COALESCE_MS", 3000)
    seen = {}

    async def script(turn):
        await turn.text("Sure")
        await asyncio.sleep(0.8)
        seen["later"] = time.monotonic()
        await turn.text(", it is ten past nine.")
    h = await make(script)
    send = asyncio.create_task(h.send("What time is it?"))
    first = await h.wait_for(lambda: h.events_of_type("text"), what="first text")
    first_at = time.monotonic()
    _, events = await send
    assert first[0]["data"]["delta"] == "Sure"
    assert first_at < seen["later"]
    assert "".join(e["data"]["delta"] for e in events if e["type"] == "text") == "Sure, it is ten past nine."


async def test_the_sentence_before_a_tool_call_is_on_the_stream_before_the_tool_runs(make):
    marks = {}

    async def script(turn):
        marks["system"] = turn.body["system"][0]["text"]
        await turn.text("Sure, switching to bedside mode.")
        marks["tool_call"] = time.monotonic()
        await turn.tool("device_action", {"tool": "self.screen.set_bedside", "args": {"mode": "night"}})
        await turn.text("Done.")
    h = await make(script)
    send = asyncio.create_task(h.send("Set bedside mode"))
    first = await h.wait_for(lambda: h.events_of_type("text"), what="first text")
    first_at = time.monotonic()
    assert h.events_of_type("device_action") == [] or first[0]["seq"] < h.events_of_type("device_action")[0]["seq"]
    event = await _next_action(h)
    await asyncio.sleep(0.5)
    resp = await _post_result(h, event["chat_id"], event["data"]["action_id"], {"ok": True, "result": "{}"})
    assert resp.status_code == 200
    _, events = await send
    types = [e["type"] for e in events]
    assert types.index("text") < types.index("device_action")
    assert first[0]["data"]["delta"] == "Sure, "
    assert first_at - marks["tool_call"] < 0.3
    assert "Before your first tool call in a reply, say one short sentence" in marks["system"]
    assert "never begin a reply with a tool call" in marks["system"]


async def test_deltas_up_to_the_first_clause_are_not_coalesced(make, monkeypatch):
    monkeypatch.setattr(turns, "TEXT_COALESCE_MS", 3000)
    seen = {}

    async def script(turn):
        await turn.text("Okay let me set that", delay=0.05)
        await asyncio.sleep(0.5)
        seen["later"] = time.monotonic()
        await turn.text("up. And the rest")
    h = await make(script)
    send = asyncio.create_task(h.send("Set bedside mode"))
    head = await h.wait_for(lambda: len(h.events_of_type("text")) >= 5 and h.events_of_type("text"), what="5 deltas")
    head_at = time.monotonic()
    _, events = await send
    assert [e["data"]["delta"] for e in head[:5]] == ["Okay ", "let ", "me ", "set ", "that"]
    assert head_at < seen["later"]
    assert [e["data"]["delta"] for e in events if e["type"] == "text"] == [
        "Okay ", "let ", "me ", "set ", "that", "up. ", "And the rest"]
