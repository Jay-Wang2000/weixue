"""Stop lexicon, interrupt route, owned tokens denied, uncorrelated wording, unknown side effects."""
import asyncio
import json
import uuid

import pytest

from fake_hamcp import FakeHaMcp
from weixue_agent import ha_adapter

LIGHT = {"domain": "light", "service": "turn_on", "entity_id": "light.living_room_ceiling_lights"}


def working(gate):
    async def script(turn):
        await turn.text("On it.")
        await turn.tool("ha_search", {"query": "lights"})
        await gate.wait()
        await turn.tool("ha_get_state", {"entity_id": "light.living_room_ceiling_lights"})
        await turn.text("Done.")
    return script


async def test_stop_lexicon_cancels_the_task_and_speaks_stopped(make):
    gate = asyncio.Event()
    h = await make(working(gate))
    _, first = await h.send("Check every light in the house")
    task_id = first[0]["task_id"]
    _, stop = await h.send("Stop")
    assert stop[0]["data"]["routed"] == "cancel"
    assert "".join(e["data"]["delta"] for e in stop if e["type"] == "text") == "Stopped."
    assert stop[-1]["data"]["reason"] == "ack"
    task = h.store.task(task_id)
    assert task["state"] == "cancelled" and task["outcome"] == "none"
    cancelled = h.events(first[0]["chat_id"], "cancelled")[0]["data"]
    assert cancelled["reason"] == "voice" and cancelled["side_effects"] == []
    gate.set()
    await asyncio.sleep(0.1)
    assert [c["tool"] for c in h.fake_ha.calls] == ["ha_search"]
    await h.send("what next")
    assert any("[note: the user stopped your previous work]" in p and p.endswith("what next")
               for p in h.api.prompts)
    tail = h.transcript(first[0]["chat_id"]).rows()
    assert sum(1 for r in tail if r["rewritten"]) == 2


async def test_chinese_stop_word_and_other_phrasings_steer(make):
    gate = asyncio.Event()
    h = await make(working(gate))
    _, first = await h.send("检查所有的灯")
    _, steer = await h.send("please stop doing that and tell me the time")
    assert steer[0]["data"]["routed"] == "steer"
    _, stop = await h.send("算了")
    assert stop[0]["data"]["routed"] == "cancel"
    assert "".join(e["data"]["delta"] for e in stop if e["type"] == "text") == "好，停了。"


async def test_unknown_side_effect_is_mentioned_and_task_outcome_unknown(make, monkeypatch):
    monkeypatch.setattr(ha_adapter, "HA_CALL_BUDGET_S", 5)

    async def script(turn):
        await turn.text("Turning it on.")
        await turn.tool("ha_call_service", LIGHT)
        await turn.text("Done.")
    h = await make(script)
    h.fake_ha.call_delay = 1.0
    _, first = await h.send("Turn on the light")
    await h.wait_for(lambda: h.fake_ha.calls, what="sent")
    _, stop = await h.send("cancel")
    spoken = "".join(e["data"]["delta"] for e in stop if e["type"] == "text")
    assert spoken == "Stopped. Some of it may already have happened."
    task = h.store.task(first[0]["task_id"])
    assert task["state"] == "cancelled" and task["outcome"] == "unknown" and not task["retry_allowed"]


async def test_interrupt_route_is_idempotent_by_decision_id(make):
    gate = asyncio.Event()
    h = await make(working(gate))
    _, first = await h.send("Check every light")
    body = {"decision_id": str(uuid.uuid4()), "reason": "panel"}
    one = await h.http.post(f"/v1/chats/{first[0]['chat_id']}/interrupt", json=body)
    two = await h.http.post(f"/v1/chats/{first[0]['chat_id']}/interrupt", json=body)
    assert one.json()["task"]["state"] == "cancelled"
    assert two.json()["duplicate"] is True
    assert h.events(first[0]["chat_id"], "cancelled")[0]["data"]["reason"] == "panel"


async def test_uncorrelated_hold_is_never_reported_cancelled(make, monkeypatch):

    async def script(turn):
        await turn.text("Turning on the plug.")
        await turn.tool("ha_call_service", {"domain": "switch", "service": "turn_on",
                                                     "entity_id": "switch.working_desk_plug"})
        await turn.text("Needs your OK.")
    fake = FakeHaMcp(wait_seconds=30)
    fake.list_pending_hidden = True
    h = await make(script, fake_ha=fake)
    _, first = await h.send("turn on the plug")
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    resp = await h.http.post(f"/v1/chats/{first[0]['chat_id']}/interrupt",
                             json={"decision_id": str(uuid.uuid4()), "reason": "panel"})
    assert resp.status_code == 200
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    assert row["state"] == "uncorrelated"
    cards = [json.loads(d["payload_json"]) for d in h.deliveries(slot=f"int:{row['interaction_id']}")]
    assert cards[-1]["summary"] == "This may still be waiting in the Home Assistant panel."
    assert h.fake_ha.decisions == []
