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
    monkeypatch.setattr(interactions, "HA_PENDING_POLL_S", 0.05)


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


async def test_explicit_phrase_approves_a_reversible_card_in_the_window(make):
    h = await make(holding(PLUG), fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("turn on the desk plug")
    assert first[-1]["data"]["reason"] == "question"
    assert "I need your OK on the panel to turn on switch.working_desk_plug." in \
        "".join(e["data"]["delta"] for e in first if e["type"] == "text")
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    await h.poll()
    _, yes = await h.send("yes")
    assert yes[0]["data"]["routed"] == "approval"
    assert "Say approve to turn on switch.working_desk_plug" in "".join(e["data"]["delta"] for e in yes if e["type"] == "text")
    assert h.fake_ha.decisions == []
    _, ok = await h.send("approve")
    assert ok[0]["data"]["routed"] == "approval"
    assert h.fake_ha.decisions and h.fake_ha.decisions[0]["decision"] == "approved"
    await h.wait_task(task_id, "done")


async def test_irreversible_actions_are_tap_only_but_voice_can_deny(make):
    h = await make(holding(AUTOMATION, tool="ha_config_set_automation"), fake_ha=FakeHaMcp(wait_seconds=30))
    _, first = await h.send("make a restart automation")
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    await h.poll()
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
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
