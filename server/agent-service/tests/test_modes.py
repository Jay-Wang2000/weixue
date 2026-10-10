"""Per-turn model (decision 39): quick on Sonnet 5.5 at effort low, task on Fable 5.1 at effort high, switched
at a tool boundary by start_task or the quick round limit; the memory tool only on the task model."""
import asyncio

from fake_api import blocks, last_user

SONNET, FABLE = "claude-sonnet-5-5", "claude-fable-5-1"


def names(body):
    return {t["name"] for t in body["tools"]}


async def test_quick_turn_on_sonnet_without_memory(make):
    h = await make()
    _, events = await h.send("What time is it?")
    await h.wait_task(events[0]["task_id"], "done")
    body = h.api.requests[0]
    assert body["model"] == SONNET and body["output_config"] == {"effort": "low"}
    assert "memory" not in names(body)
    assert not any(m["role"] == "system" and m.get("output_config") for m in body["messages"])


async def test_start_task_switches_to_fable_with_an_effort_message_before_the_tool_results(make):
    async def script(turn):
        if turn.model == SONNET and "time" in turn.user_text:
            await turn.text("It's late.")
            return
        await turn.text("On it, I'll draft it.")
        await turn.tool("start_task", {"title": "Sunset automation"})
        await turn.tool("ha_get_history", {"entity_id": "light.living_room_ceiling_lights"})
        await turn.text(f"Drafted on {turn.model}.")
    h = await make(script)
    _, events = await h.send("Draft a sunset automation for the living room lights")
    task = await h.wait_task(events[0]["task_id"], "done")
    first, second, third = h.api.requests[:3]
    assert first["model"] == SONNET and second["model"] == FABLE and third["model"] == FABLE
    assert "memory" in names(second) and "memory" not in names(first)
    tail = second["messages"][-3:]
    assert tail[0]["role"] == "assistant" and tail[0]["content"][-1]["name"] == "start_task"
    assert tail[1] == {"role": "system", "content": [], "output_config": {"effort": "high"}}
    assert [b["type"] for b in blocks(tail[2])] == ["tool_result"]
    assert task["mode"] == "task" and task["title"] == "Sunset automation"
    assert "".join(e["data"]["delta"] for e in events if e["type"] == "text").startswith("On it")
    _, quick = await h.send("What time is it?")
    body = h.api.requests[-1]
    assert body["model"] == SONNET
    efforts = [m["output_config"]["effort"] for m in body["messages"] if m.get("output_config")]
    assert efforts == ["high", "low"]
    assert h.store.chat(task["chat_id"])["mode"] == "quick"


async def test_quick_round_limit_escalates_to_the_task_model(make):
    async def script(turn):
        for i in range(4):
            await turn.tool("ha_search", {"query": f"q{i}"})
        await turn.text("Done.")
    h = await make(script)
    _, events = await h.send("Check everything")
    task = await h.wait_task(events[0]["task_id"], "done")
    models = [b["model"] for b in h.api.requests]
    assert models == [SONNET, SONNET, SONNET, FABLE, FABLE]
    assert task["mode"] == "task"


async def test_steer_inside_a_running_loop_keeps_its_model(make):
    async def script(turn):
        await turn.text("On it.")
        await turn.tool("start_task", {"title": "Long"})
        await turn.tool("ha_search", {"query": "x"})
        await turn.tool("ha_get_state", {"entity_id": "light.a"})
        await turn.text("Done.")
    h = await make(script)
    h.fake_ha.call_delay = 0.2
    first = asyncio.create_task(h.send("Do the long thing"))
    await h.wait_for(lambda: h.fake_ha.calls, what="task tool call")
    await h.send("How is it going?")
    await first
    steered = [b for b in h.api.requests if "How is it going?" in
               "".join(x.get("text", "") for x in blocks(last_user(b)))]
    assert steered and all(b["model"] == FABLE for b in steered)


async def test_start_task_without_spoken_words_speaks_the_canned_ack_at_once(make):
    async def script(turn):
        await turn.tool("start_task", {"title": "Research"})
        await turn.text("Here is what I found.")
    h = await make(script)
    _, events = await h.send("Research the bins schedule")
    texts = [e["data"]["delta"] for e in events if e["type"] == "text"]
    assert texts[0] == "On it."
