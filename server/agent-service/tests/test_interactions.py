"""ask_user: inline cue and voice answer, late answers, panel answers."""
import asyncio
import json
import uuid

import pytest

from weixue_agent import interactions


@pytest.fixture(autouse=True)
def window(monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 2.0)


def asking(seen):
    async def script(turn):
        if turn.user_text.startswith("[answer to your earlier question"):
            seen.append(("late", turn.user_text))
            await turn.text("Got it, Thursday.")
            return
        answer = await turn.ask("Which day are the bins?", ["Monday", "Thursday"])
        seen.append(("tool", answer))
        await turn.text(f"Okay: {answer}.")
    return script


async def test_question_spoken_inline_and_answered_by_voice_in_the_window(make):
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Remind me about the bins")
    assert first[-1]["data"]["reason"] == "question"
    assert "Which day are the bins?" in "".join(e["data"]["delta"] for e in first if e["type"] == "text")
    task_id = first[0]["task_id"]
    assert h.store.task(task_id)["state"] == "needs_input"
    _, answer = await h.send("Thursday")
    assert answer[0]["data"]["routed"] == "answer" and answer[0]["turn_id"] == first[0]["turn_id"]
    assert "Okay:" in "".join(e["data"]["delta"] for e in answer if e["type"] == "text")
    assert seen[0][1] == "The user answered: Thursday"
    await h.wait_task(task_id, "done")


async def test_unanswered_question_is_denied_and_answered_later_as_a_new_turn(make, monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 0.3)
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Remind me about the bins")
    task_id = first[0]["task_id"]
    await h.wait_for(lambda: seen, what="tool result")
    assert seen[0][1] == interactions.LATE_ANSWER
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    assert row["state"] == "pending"
    await h.wait_task(task_id, "needs_input")
    spoken = [d for d in h.deliveries(slot=f"int:{row['interaction_id']}") if d["speak"]]
    assert spoken and spoken[-1]["tier"] == "WHEN_IDLE"
    resp = await h.http.post(f"/v1/chats/{first[0]['chat_id']}/answer",
                             json={"interaction_id": row["interaction_id"], "decision_id": str(uuid.uuid4()),
                                   "option_ids": ["opt2"], "via": "panel"})
    assert resp.status_code == 200, resp.text
    await h.wait_task(task_id, "done")
    assert seen[-1] == ("late", '[answer to your earlier question "Which day are the bins?"]: Thursday')


async def test_question_after_reply_end_is_denied_at_once(make):
    seen = []
    gate = asyncio.Event()

    async def script(turn):
        await turn.text("Let me look.")
        await turn.tool("ha_search", {"query": "bins"})
        await gate.wait()
        seen.append(await turn.ask("Which bin?", ["Green", "Black"]))
        await turn.text("Waiting for your answer.")
    h = await make(script)
    _, first = await h.send("bins")
    assert first[-1]["data"]["reason"] == "hold_elapsed"
    gate.set()
    await h.wait_task(first[0]["task_id"], "needs_input")
    assert seen == [interactions.LATE_ANSWER]


async def test_panel_answer_with_unknown_option_is_rejected(make, monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 0.3)
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Remind me about the bins")
    await h.wait_for(lambda: seen, what="denied")
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (first[0]["task_id"],))
    resp = await h.http.post(f"/v1/chats/{first[0]['chat_id']}/answer",
                             json={"interaction_id": row["interaction_id"], "decision_id": str(uuid.uuid4()),
                                   "option_ids": ["nope"], "via": "panel"})
    assert resp.status_code == 400
