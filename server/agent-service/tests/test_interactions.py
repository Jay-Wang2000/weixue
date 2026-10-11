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


@pytest.mark.parametrize("args, why", [
    ({"question": "Which light?"}, "no options given"),
    ({"question": "Which light?", "options": ["Living room"]}, "1 options given"),
    ({"question": "Which light?", "options": ["A", "B", "C", "D", "E"]}, "5 options given"),
    ({"question": "Which light?", "options": ["Living room", "  "]}, "an option is empty"),
    ({"question": "Which light?", "options": ["Living room", "living  room"]}, "two options are the same"),
    ({"question": "Which light?", "options": ["Living room ceiling", "Bedroom"]}, "an option is longer than 12"),
    ({"question": "哪个灯？", "options": ["客厅吊顶灯", "卧室"]}, "an option is longer than 12"),
    ({"question": "W" * 121, "options": ["Yes", "No"]}, "longer than 120"),
    ({"question": "", "options": ["Yes", "No"]}, "no question given"),
    ({"question": "What would you like me to do next?", "options": ["Lights", "Music"]}, "open-ended"),
    ({"question": "Anything else?", "options": ["Yes", "No"]}, "open-ended"),
    ({"question": "Let me know which one, okay?", "options": ["Red", "Blue"]}, "open-ended"),
    ({"question": "还有什么需要我做的吗？", "options": ["开灯", "关灯"]}, "open-ended"),
])
def test_check_question_rejects(args, why):
    question, reason = interactions.check_question(args)
    assert question is None and why in reason


def test_check_question_accepts_a_real_choice():
    question, reason = interactions.check_question(
        {"question": " Which  light? ", "options": [" Living room ", "Bedroom", "客厅灯"], "multi_select": True})
    assert reason is None
    assert question == {"text": "Which light?", "multi_select": True,
                        "options": [{"id": "opt1", "label": "Living room"}, {"id": "opt2", "label": "Bedroom"},
                                    {"id": "opt3", "label": "客厅灯"}]}


async def test_rejected_ask_user_creates_no_card_and_the_turn_ends_done(make):
    seen = []

    async def script(turn):
        seen.append(await turn.tool("ask_user", {"question": "What would you like me to do next?"}))
        await turn.text("All set. Anything else?")
    h = await make(script)
    _, events = await h.send("Turn on the lights")
    assert seen[0].startswith("Not asked: an open-ended question") and "end the turn" in seen[0]
    assert events[-1]["data"]["reason"] == "turn_done"
    await h.wait_task(events[0]["task_id"], "done")
    assert h.store.scalar("SELECT COUNT(*) FROM interactions") == 0
    assert not h.events_of_type("needs_input")
    assert not [d for d in h.deliveries() if d["slot"].startswith("int:")]


def test_no_question_interaction_without_options(make):
    with pytest.raises(ValueError):
        interactions.Interactions._new(object.__new__(interactions.Interactions), "tsk_x", "input", "sdk", None,
                                       question={"text": "Which day?", "options": []})


async def test_real_choice_makes_one_card_with_option_buttons_and_a_tap_answers_it(make, monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 0.3)
    seen = []

    async def script(turn):
        if turn.user_text.startswith("[answer to your earlier question"):
            seen.append(turn.user_text)
            await turn.text("Bedroom light is on.")
            return
        seen.append(await turn.ask("Which light?", ["Living room", "Bedroom"]))
        turn.thinking("Waiting for the user to pick a light.")
        await turn.text("Tell me when you've picked.")
    h = await make(script)
    _, first = await h.send("Turn on the light")
    task_id, chat_id = first[0]["task_id"], first[0]["chat_id"]
    await h.wait_task(task_id, "needs_input")
    rows = h.store.all("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    assert len(rows) == 1 and rows[0]["kind"] == "input"
    cards = [d for d in h.deliveries(slot=f"int:{rows[0]['interaction_id']}") if d["state"] in ("pending", "offered")]
    assert len(cards) == 1
    payload = json.loads(cards[0]["payload_json"])
    assert payload["state"] == "needs_input" and payload["summary"] == "Which light?"
    assert payload["options"] == [{"id": "opt1", "label": "Living room"}, {"id": "opt2", "label": "Bedroom"}]
    await h.wait_for(lambda: len(seen) == 1, what="the late ask result")
    await h.idle(chat_id)
    after = [d for d in h.deliveries(task_id=task_id) if d["gseq"] > cards[0]["gseq"]]
    assert not [d for d in after if json.loads(d["payload_json"])["state"] == "progress"]
    resp = await h.http.post(f"/v1/chats/{chat_id}/answer",
                             json={"interaction_id": rows[0]["interaction_id"], "decision_id": str(uuid.uuid4()),
                                   "option_ids": ["opt2"], "via": "panel"})
    assert resp.status_code == 200, resp.text
    await h.wait_task(task_id, "done")
    assert seen[-1] == '[answer to your earlier question "Which light?"]: Bedroom'


async def test_spoken_answer_after_the_window_answers_the_open_question_as_other(make, monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 0.3)
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Remind me about the bins")
    task_id = first[0]["task_id"]
    await h.wait_for(lambda: seen, what="tool result")
    await h.wait_task(task_id, "needs_input")
    h.store.update("devices", "device_id", "sim-device-01", {"voice_window_until": None})
    _, answer = await h.send("Wednesday actually")
    assert answer[0]["data"]["routed"] == "answer" and answer[0]["task_id"] == task_id
    await h.wait_task(task_id, "done")
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (task_id,))
    assert row["state"] == "answered" and json.loads(row["decision_json"]) == {"answer": "Wednesday actually"}
    assert seen[-1] == ("late", '[answer to your earlier question "Which day are the bins?"]: Wednesday actually')
