"""Spoken interaction window, reversible-only phrases, repeat-back, tap-only, continue, parallel chats."""
import asyncio
import json
import uuid

import pytest

from fake_hamcp import FakeHaMcp
from weixue_agent import interactions, routing

PLUG = {"domain": "switch", "service": "turn_on", "entity_id": "switch.working_desk_plug"}
AUTOMATION = {"config": {"alias": "Restart", "actions": [{"action": "homeassistant.restart"}]}}


@pytest.fixture(autouse=True)
def window(monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 5.0)


def holding(args, tool="ha_call_service"):
    async def script(turn):
        if turn.user_text.startswith("[event approval"):
            await turn.text("Done." if "executed" in turn.user_text else "Okay, not doing it.")
            return
        await turn.tool(tool, args)
        await turn.text("Waiting for your OK.")
    return script


async def test_lexicon_normalisation():
    assert routing.is_stop("Stop!") and routing.is_stop(" never mind. ") and routing.is_stop("算了。")
    assert not routing.is_stop("stop the music")
    assert routing.voice_decision("Approve.") == "approve" and routing.voice_decision("do it") == "approve"
    assert routing.voice_decision("同意") == "approve" and routing.voice_decision("不要") == "deny"
    assert routing.voice_decision("yes") == "yes" and routing.voice_decision("好") == "yes"
    assert routing.voice_decision("approve it please") is None
    assert routing.detect_lang("开灯") == "zh" and routing.detect_lang("hi") == "en"


def spoken(events):
    return "".join(e["data"]["delta"] for e in events if e["type"] == "text")


async def own_and_play(h, row):
    """The original request returns the token; the re-announced card is played, which opens the voice window."""
    h.fake_ha.elapse_wait()
    await h.wait_for(lambda: h.store.interaction(row["interaction_id"])["state"] == "pending", what="owned")
    delivery = [d for d in h.deliveries(slot=f"int:{row['interaction_id']}") if d["speak"]][-1]
    offer = h.service.outbox.offer(delivery)
    for outcome in ("accepted", "played"):
        resp = await h.http.post(f"/v1/deliveries/{delivery['delivery_id']}/ack",
                                 json={"lease_id": offer["lease_id"], "outcome": outcome})
        assert resp.status_code == 200, resp.text
    return delivery


async def test_explicit_phrase_approves_a_reversible_card_in_the_window(make):
    h = await make(holding(PLUG), fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("turn on the desk plug")
    assert first[-1]["data"]["reason"] == "question"
    assert "To turn on switch.working_desk_plug, approve it in the Home Assistant panel." in spoken(first)
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    _, early = await h.send("approve")
    assert early[0]["data"]["routed"] == "approval" and h.fake_ha.decisions == []
    assert spoken(early) == interactions.PANEL_CUE["en"]
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    delivery = await own_and_play(h, row)
    assert "I need your OK on the panel to turn on switch.working_desk_plug." == delivery["speak"]
    _, yes = await h.send("yes")
    assert yes[0]["data"]["routed"] == "approval"
    assert "Say approve to turn on switch.working_desk_plug" in spoken(yes)
    assert h.fake_ha.decisions == []
    _, ok = await h.send("approve")
    assert ok[0]["data"]["routed"] == "approval"
    assert h.fake_ha.decisions and h.fake_ha.decisions[0]["decision"] == "approved"
    await h.wait_task(task_id, "done")
    assert len(h.fake_ha.executed) == 1


async def test_irreversible_actions_are_tap_only_but_voice_can_deny(make):
    h = await make(holding(AUTOMATION, tool="ha_config_set_automation"), fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("make a restart automation")
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    await own_and_play(h, row)
    row = h.store.interaction(row["interaction_id"])
    assert row["voice_can_approve"] == 0
    _, approve = await h.send("approve")
    assert approve[0]["data"]["routed"] == "approval" and h.fake_ha.decisions == []
    resp = await h.http.post(f"/v1/chats/{first[0]['chat_id']}/approve",
                             json={"interaction_id": row["interaction_id"], "decision_id": str(uuid.uuid4()), "via": "voice"})
    assert resp.status_code == 409
    _, deny = await h.send("no")
    assert h.fake_ha.decisions[0]["decision"] == "denied"
    task = await h.wait_task(task_id, "done")
    assert task["outcome"] == "denied"


async def test_other_text_in_the_window_falls_through_to_continue(make):
    h = await make(holding(PLUG), fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("turn on the desk plug")
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    _, other = await h.send("what is the weather")
    assert other[0]["data"]["routed"] == "continue" and other[0]["task_id"] == task_id


async def test_second_chat_runs_in_parallel_without_interrupting_the_first(make):
    gate = asyncio.Event()

    async def script(turn):
        if "weather" in turn.prompt:
            await turn.text("Sunny.")
            return
        await turn.text("Working on it.")
        await turn.tool("ha_search", {"query": "x"})
        await gate.wait()
        await turn.text("Done.")
    h = await make(script)
    _, first = await h.send("long task")
    other = await h.http.post("/v1/chats", json={"device_id": "sim-device-01"}, headers={"Idempotency-Key": "k"})
    chat_b = other.json()["chat_id"]
    _, second = await h.send("What's the weather like?", chat=chat_b)
    assert second[0]["data"]["routed"] == "new_task" and second[-1]["data"]["reason"] == "turn_done"
    assert not h.events(first[0]["chat_id"], "interrupted")
    status = (await h.http.get("/v1/status")).json()
    assert status["live_clients"] == 2 and status["turns_in_flight"] == 1
    gate.set()
    await h.wait_task(first[0]["task_id"], "done")


async def test_a_chat_switch_closes_the_spoken_window_of_another_chat(make):
    h = await make(holding(PLUG), fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("turn on the desk plug")
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    other = await h.http.post("/v1/chats", json={"device_id": "sim-device-01"}, headers={"Idempotency-Key": "k"})
    _, said = await h.send("approve")
    assert said[0]["data"]["routed"] == "new_task" and said[0]["chat_id"] == other.json()["chat_id"]
    assert h.fake_ha.decisions == [], "the other chat's card is not approved by voice"
