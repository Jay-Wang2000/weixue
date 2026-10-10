"""STREAMS_MAX: a new turn waits QUEUE_WAIT_MS for a free stream, then 503 with a spoken cue; idle actors are
dropped from memory and reload from SQLite, busy ones never."""
import asyncio

from weixue_agent import routing, turns


async def test_stream_cap_answers_503_busy_with_a_spoken_cue(make, monkeypatch):
    monkeypatch.setattr(turns, "STREAMS_MAX", 1)
    gate = asyncio.Event()

    async def script(turn):
        if "quick" in turn.user_text:
            await turn.text("Quick answer.")
            return
        await turn.text("Busy.")
        await turn.tool("ha_search", {"query": "x"})
        await gate.wait()
        await turn.text("Done.")
    h = await make(script)
    first_task = asyncio.create_task(h.send("long work"))
    await h.wait_for(lambda: h.engine.streams_in_flight() == 1, what="first turn running")
    other = (await h.http.post("/v1/chats", json={"device_id": "sim-device-01"},
                               headers={"Idempotency-Key": "b1"})).json()
    resp = await h.http.post(f"/v1/chats/{other['chat_id']}/messages", json={"device_id": "sim-device-01", "text": "quick"},
                             headers={"Idempotency-Key": "b2"})
    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["code"] == "unavailable" and body["detail"]["speak"] == routing.BUSY["en"]
    gate.set()
    _, first = await first_task
    await h.wait_task(first[0]["task_id"], "done")
    _, ok = await h.send("quick one", chat=other["chat_id"])
    assert ok[-1]["data"]["reason"] == "turn_done"


async def test_idle_actors_are_evicted_and_reload_from_sqlite_busy_ones_never(make):
    gate = asyncio.Event()

    async def script(turn):
        if "remember" in turn.user_text:
            await turn.text("Working.")
            await turn.tool("ha_search", {"query": "x"})
            await gate.wait()
        await turn.text(f"Seen {sum(1 for m in turn.body['messages'] if m['role'] == 'user')} user messages.")
    h = await make(script)
    first = await h.send_json("hello")
    await h.idle(first["chat_id"])
    other = (await h.http.post("/v1/chats", json={"device_id": "sim-device-01"},
                               headers={"Idempotency-Key": "c"})).json()
    busy_task = asyncio.create_task(h.send("remember this", chat=other["chat_id"]))
    await h.wait_for(lambda: h.engine.turn_in_flight(other["chat_id"]), what="busy")
    assert h.engine.reap_idle(idle_s=0) == 1
    assert first["chat_id"] not in h.engine.actors and other["chat_id"] in h.engine.actors
    _, again = await h.send("again", chat=first["chat_id"])
    assert "Seen 2 user messages." in "".join(e["data"]["delta"] for e in again if e["type"] == "text")
    gate.set()
    await busy_task
