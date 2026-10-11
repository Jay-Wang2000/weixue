"""Append-only transcript: each request keeps the previous one's messages through its last assistant message
(only the uncommitted tail may change), pause_turn resumes, fallback echo form, compaction epochs, frozen
system and tools across restarts."""
import asyncio
import json
import uuid

from weixue_agent import actor
from weixue_agent.transcript import echo_form


def assert_append_only(requests):
    """Every request starts with the previous request's messages up to and including its last assistant message."""
    for a, b in zip(requests, requests[1:]):
        if a.get("compaction") or b.get("compaction") or b["messages"][0].get("content", [{}])[0].get("type") == "compaction":
            continue
        last = max((i for i, m in enumerate(a["messages"]) if m["role"] == "assistant"), default=-1)
        assert b["messages"][:last + 1] == a["messages"][:last + 1]
        assert len(b["messages"]) >= len(a["messages"]) or last + 1 < len(a["messages"])


def stable_prompt(requests):
    by_model = {}
    for body in requests:
        if body.get("compaction"):
            continue
        key = body["model"]
        seen = by_model.setdefault(key, (body["system"], body["tools"]))
        assert (body["system"], body["tools"]) == seen


async def test_requests_are_append_only_across_tool_rounds_steers_cancels_and_restarts(make):
    gate = asyncio.Event()

    async def script(turn):
        if turn.user_text == "Tell me something slow":
            await turn.text("Thinking about it at length right now", delay=0.1)
            return
        turn.thinking("Looking it up.")
        await turn.text("Checking.")
        await turn.tool("ha_search", {"query": "lights"})
        await turn.text("Found them.")
        await turn.tool("ha_get_state", {"entity_id": "light.living_room_ceiling_lights"})
        await turn.text("The light is off.")
    h = await make(script)
    h.fake_ha.call_delay = 0.15
    _, first = await h.send("Is the light on?")
    chat_id = first[0]["chat_id"]
    await h.idle(chat_id)
    one = asyncio.create_task(h.send("Is the light on now?"))
    await h.wait_for(lambda: len(h.fake_ha.calls) >= 3, what="second turn tool call")
    await h.send("And the kitchen?")
    await one
    await h.idle(chat_id)
    slow = asyncio.create_task(h.send("Tell me something slow"))
    await h.wait_for(lambda: h.engine.current_run(chat_id) and h.engine.current_run(chat_id).heard, what="text")
    await h.send("stop")
    await slow
    await h.restart()
    await h.send("Is the light on?")
    assert_append_only(h.api.requests)
    stable_prompt(h.api.requests)
    rows = h.transcript(chat_id).rows()
    assert [r["seq"] for r in rows] == list(range(len(rows)))
    assert all("at length" not in r["content_json"] for r in rows)


async def test_pause_turn_is_resumed_once_and_the_paused_message_kept(make):
    async def script(turn):
        await turn.text("Searching.")
        await turn.pause()
        await turn.text("Here is the weather.")
    h = await make(script)
    _, events = await h.send("What's the weather?")
    task = await h.wait_task(events[0]["task_id"], "done")
    assert len(h.api.requests) == 2
    paused = h.api.requests[1]["messages"][-1]
    assert paused["role"] == "assistant" and paused["content"][-1]["type"] == "server_tool_use"
    rows = h.transcript(task["chat_id"]).rows()
    assert [r["role"] for r in rows] == ["user", "system", "assistant", "assistant"]


def test_fallback_echo_form_keeps_only_text_and_paired_server_blocks_before_the_boundary():
    content = [{"type": "thinking", "thinking": "", "signature": "s"},
               {"type": "text", "text": "Partial"},
               {"type": "server_tool_use", "id": "srv1", "name": "web_search", "input": {}},
               {"type": "web_search_tool_result", "tool_use_id": "srv1", "content": []},
               {"type": "server_tool_use", "id": "srv2", "name": "web_search", "input": {}},
               {"type": "tool_use", "id": "t1", "name": "ha_search", "input": {}},
               {"type": "fallback", "from": {"model": "claude-fable-5-1"}, "to": {"model": "claude-opus-5"}},
               {"type": "thinking", "thinking": "", "signature": "s2"},
               {"type": "text", "text": "Rest"}]
    kept = echo_form(content)
    assert [b["type"] for b in kept] == ["text", "server_tool_use", "web_search_tool_result", "fallback", "thinking",
                                         "text"]
    assert echo_form(content[:2]) == content[:2]


async def test_compaction_starts_a_new_epoch_with_a_fresh_prompt_row(make, monkeypatch):
    monkeypatch.setattr(actor, "COMPACT_AT_TOKENS", 50)

    async def script(turn):
        await turn.text("Answer.")
    h = await make(script)
    _, first = await h.send("hello")
    chat_id = first[0]["chat_id"]
    await h.idle(chat_id)
    assert h.api.compactions == 1
    compaction = h.api.requests[1]
    assert compaction["compaction"]["type"] == "summarize" and "open task" in compaction["compaction"]["instructions"]
    assert h.store.chat(chat_id)["epoch"] == 1
    rows = h.transcript(chat_id).rows()
    assert rows[0]["role"] == "assistant" and '"compaction"' in rows[0]["content_json"]
    assert h.store.scalar("SELECT COUNT(*) FROM chat_prompts WHERE chat_id = ?", (chat_id,)) == 1
    await h.send("again")
    after = h.api.requests[-1]["messages"]
    assert after[0]["content"][0]["type"] == "compaction" and after[1]["content"][0]["text"] == "again"
    assert h.store.scalar("SELECT COUNT(*) FROM chat_prompts WHERE chat_id = ?", (chat_id,)) == 2


def user_only_results(message):
    return message["role"] == "user" and all(b.get("type") == "tool_result" for b in message["content"])


async def test_mixed_turn_keeps_the_server_call_and_sends_only_client_results(make):
    """F07: the pending server tool stays; the next message holds the client results alone, and a steer that
    arrived meanwhile follows once the server tool settled."""
    async def script(turn):
        if "and the news" in turn.user_text:
            await turn.text("And the news is quiet.")
            return
        await turn.text("Looking.")
        turn.response.block({"type": "tool_use", "id": "toolu_pair", "name": "ha_search", "input": {}},
                            [{"type": "input_json_delta", "partial_json": '{"query": "x"}'}])
        server = turn.server_tool_unpaired()
        turn.response.finish("tool_use")
        await turn._wait_next(["toolu_pair"])
        turn.server_result(server)
        await turn.text("Done.")
    h = await make(script)
    h.fake_ha.call_delay = 0.4
    first = asyncio.create_task(h.send("Search"))
    await h.wait_for(lambda: h.fake_ha.calls, what="client tool running")
    _, steer = await h.send("and the news")
    _, events = await first
    await h.idle(events[0]["chat_id"])
    assert h.api.violations == []
    continuation = h.api.requests[1]["messages"]
    assistant = [m for m in continuation if m["role"] == "assistant"][0]
    assert [b["type"] for b in assistant["content"]] == ["text", "tool_use", "server_tool_use"]
    assert continuation[-1] == continuation[-1] and user_only_results(continuation[-1])
    assert any("and the news" in json.dumps(m) for m in h.api.requests[-1]["messages"][-3:])
    server_rows = h.store.all("SELECT status FROM tool_calls WHERE kind = 'server'")
    assert [r["status"] for r in server_rows] == ["ok"]


async def test_cancel_after_pause_turn_settles_the_server_tool_before_the_next_message(make):
    resumed = asyncio.Event()

    async def script(turn):
        last = turn.body["messages"][-1]
        if last["role"] == "assistant":
            pending = [b["id"] for b in last["content"] if b["type"] == "server_tool_use"]
            turn.server_result(pending[-1])
            await turn.text("It is sunny.")
            return
        if turn.user_text == "hello":
            await turn.text("Hi.")
            return
        await turn.text("Searching.")
        turn.response.block({"type": "server_tool_use", "id": "srvtoolu_paused", "name": "web_search", "input": {}},
                            [{"type": "input_json_delta", "partial_json": '{"query": "long"}'}])
        turn.response.finish("pause_turn")
        await turn._wait_next(["srvtoolu_paused"])
        resumed.set()
        await asyncio.sleep(30)
    h = await make(script)
    _, events = await h.send("What's the weather?")
    chat_id = events[0]["chat_id"]
    await asyncio.wait_for(resumed.wait(), 5)
    resp = await h.http.post(f"/v1/chats/{chat_id}/interrupt", json={"decision_id": str(uuid.uuid4()), "reason": "panel"})
    assert resp.status_code == 200
    await h.idle(chat_id)
    _, hello = await h.send("hello")
    await h.idle(chat_id)
    assert h.api.violations == []
    settle = h.api.requests[-2]["messages"]
    assert settle[-1]["role"] == "assistant" and settle[-1]["content"][-1]["id"] == "srvtoolu_paused"
    said = "".join(e["data"]["delta"] for e in hello if e["type"] == "text")
    assert "Hi." in said and "sunny" not in said
    final = h.api.requests[-1]["messages"]
    assert final[-3]["content"][0]["type"] == "web_search_tool_result"
    assert_append_only(h.api.requests)


async def test_cancel_during_a_mixed_turn_answers_the_client_call_alone_first(make, monkeypatch):
    from weixue_agent import ha_adapter
    monkeypatch.setattr(ha_adapter, "HA_CALL_BUDGET_S", 5.0)

    async def script(turn):
        if turn.user_text == "hello":
            await turn.text("Hi.")
            return
        await turn.text("Looking.")
        turn.response.block({"type": "tool_use", "id": "toolu_slow", "name": "ha_search", "input": {}},
                            [{"type": "input_json_delta", "partial_json": '{"query": "x"}'}])
        server = turn.server_tool_unpaired()
        turn.response.finish("tool_use")
        await turn._wait_next(["toolu_slow"])
        turn.server_result(server)
        await turn.text("Found it.")
    h = await make(script)
    h.fake_ha.call_delay = 2.0
    first = asyncio.create_task(h.send("Search"))
    await h.wait_for(lambda: h.fake_ha.calls, what="client tool running")
    _, events = await first
    chat_id = events[0]["chat_id"]
    resp = await h.http.post(f"/v1/chats/{chat_id}/interrupt", json={"decision_id": str(uuid.uuid4()), "reason": "panel"})
    assert resp.status_code == 200
    await h.idle(chat_id)
    _, hello = await h.send("hello")
    await h.idle(chat_id)
    assert h.api.violations == []
    settle = h.api.requests[-2]["messages"]
    assert user_only_results(settle[-1]) and settle[-1]["content"][0]["tool_use_id"] == "toolu_slow"
    assert settle[-1]["content"][0]["content"].startswith("not run")
    said = "".join(e["data"]["delta"] for e in hello if e["type"] == "text")
    assert "Hi." in said and "Found it" not in said
