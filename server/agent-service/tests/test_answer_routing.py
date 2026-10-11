"""Who may answer an open question: the device's voice follow-up or the answer API, never another message."""
import json
import uuid

import pytest

from weixue_agent import interactions, routing

LABELS = ["Living room", "Bedroom"]


@pytest.fixture(autouse=True)
def window(monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 2.0)


def asking(seen):
    async def script(turn):
        if turn.user_text.startswith("[answer to your earlier question"):
            seen.append(("late", turn.user_text))
            await turn.text("Bedroom is 21 degrees.")
            return
        if "Which room?" in turn.prompt or "weather" in turn.user_text or "probe" in turn.user_text:
            seen.append(("other", turn.prompt))
            await turn.text("Sunny, 18 degrees.")
            return
        answer = await turn.ask("Which room?", LABELS)
        seen.append(("tool", answer))
        await turn.text("Tell me which room." if answer == interactions.LATE_ANSWER else f"Okay: {answer}.")
    return script


def spoken(events):
    return "".join(e["data"]["delta"] for e in events if e["type"] == "text")


async def new_chat(h, switch=False):
    resp = await h.http.post("/v1/chats", json={"device_id": "sim-device-01", "switch": switch},
                             headers={"Idempotency-Key": str(uuid.uuid4())})
    assert resp.status_code == 201, resp.text
    return resp.json()["chat_id"]


def question_row(h, task_id):
    return h.store.one("SELECT * FROM interactions WHERE task_id = ? AND kind = 'input'", (task_id,))


@pytest.mark.parametrize("text, expected", [
    ("Bedroom", True), ("the bedroom please", True), ("Wednesday actually", True), ("the second one please", True),
    ("Tell me the temperature of the living room please", True), ("卧室", True), ("第二个吧", True),
    ("What is the weather like today", False), ("turn off the kitchen light now", False), ("stop", False),
    ("turn on the bedroom light and then tell me the news", False), ("今天都柏林的天气怎么样啊", False),
])
def test_answer_matching_rule(text, expected):
    assert routing.answers_question(text, LABELS) is expected


async def test_voice_follow_up_in_the_window_answers_the_held_question(make):
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Temperature in one of my rooms")
    assert first[-1]["data"]["reason"] == "question"
    _, answer = await h.send("Bedroom")
    assert answer[0]["data"]["routed"] == "answer" and answer[0]["turn_id"] == first[0]["turn_id"]
    assert seen[0] == ("tool", "The user answered: Bedroom")
    await h.wait_task(first[0]["task_id"], "done")


async def test_answer_api_with_the_interaction_id_answers_it(make, monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 0.3)
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Temperature in one of my rooms", origin="api")
    task_id, chat_id = first[0]["task_id"], first[0]["chat_id"]
    await h.wait_task(task_id, "needs_input")
    row = question_row(h, task_id)
    resp = await h.http.post(f"/v1/chats/{chat_id}/answer",
                             json={"interaction_id": row["interaction_id"], "decision_id": str(uuid.uuid4()),
                                   "option_ids": ["opt2"], "via": "api"})
    assert resp.status_code == 200, resp.text
    await h.wait_task(task_id, "done")
    assert seen[-1] == ("late", '[answer to your earlier question "Which room?"]: Bedroom')


@pytest.mark.parametrize("origin", ["api", None])
async def test_api_message_to_another_chat_never_joins_the_open_question(make, origin):
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Temperature in one of my rooms")
    household = first[0]["task_id"]
    await h.wait_for(lambda: h.store.device("sim-device-01")["voice_window_until"], what="voice window")
    probe_chat = await new_chat(h)
    _, probe = await h.send("Bedroom probe", chat=probe_chat, origin=origin)
    assert probe[0]["chat_id"] == probe_chat and probe[0]["data"]["routed"] == "new_task"
    assert probe[0]["task_id"] != household
    assert question_row(h, household)["state"] == "pending"
    assert h.store.device("sim-device-01")["voice_window_until"], "the household's voice window stays open"
    _, answer = await h.send("Bedroom")
    assert answer[0]["data"]["routed"] == "answer" and answer[0]["task_id"] == household
    await h.wait_task(household, "done")


async def test_api_message_does_not_answer_a_question_of_its_own_chat(make, monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 0.3)
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Temperature in one of my rooms")
    task_id, chat_id = first[0]["task_id"], first[0]["chat_id"]
    await h.wait_task(task_id, "needs_input")
    _, probe = await h.send("Bedroom", chat=chat_id, origin="api")
    assert probe[0]["data"]["routed"] == "continue" and probe[0]["task_id"] == task_id
    await h.wait_task(task_id, "done")
    row = question_row(h, task_id)
    assert row["state"] == "cancelled"
    assert not [s for s in seen if s[0] == "late"]
    assert 'the user did not answer your question "Which room?"' in seen[-1][1]


async def test_unrelated_voice_command_while_the_card_is_open_is_a_new_turn(make, monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 0.3)
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Temperature in one of my rooms")
    task_id = first[0]["task_id"]
    await h.wait_task(task_id, "needs_input")
    row = question_row(h, task_id)
    _, other = await h.send("What is the weather like today")
    assert other[0]["data"]["routed"] == "continue" and other[0]["task_id"] == task_id
    assert spoken(other) == "Sunny, 18 degrees."
    await h.wait_task(task_id, "done")
    assert h.store.interaction(row["interaction_id"])["state"] == "cancelled"
    assert [e for e in h.events(first[0]["chat_id"], "resolved") if e["data"]["state"] == "cancelled"]
    assert seen[-1][0] == "other" and "What is the weather like today" in seen[-1][1]


async def test_unrelated_voice_command_in_the_window_steers_and_releases_the_question(make):
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Temperature in one of my rooms")
    task_id = first[0]["task_id"]
    _, other = await h.send("What is the weather like today")
    assert other[0]["data"]["routed"] == "steer"
    assert seen[0] == ("tool", interactions.STEER_ANSWER)
    assert question_row(h, task_id)["state"] == "cancelled"


async def test_unrelated_voice_command_after_another_chats_spoken_card_goes_to_the_current_chat(make, monkeypatch):
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 0.3)
    seen = []
    h = await make(asking(seen))
    current = await new_chat(h, switch=True)
    side = await new_chat(h)
    _, first = await h.send("Temperature in one of my rooms", chat=side, origin="api")
    side_task = first[0]["task_id"]
    await h.wait_task(side_task, "needs_input")
    monkeypatch.setattr(interactions, "VOICE_WINDOW_S", 5.0)
    row = question_row(h, side_task)
    delivery = [d for d in h.deliveries(slot=f"int:{row['interaction_id']}") if d["speak"]][-1]
    offer = h.service.outbox.offer(delivery)
    for outcome in ("accepted", "played"):
        resp = await h.http.post(f"/v1/deliveries/{delivery['delivery_id']}/ack",
                                 json={"lease_id": offer["lease_id"], "outcome": outcome})
        assert resp.status_code == 200, resp.text
    assert h.store.device("sim-device-01")["voice_interaction_id"] == row["interaction_id"]
    _, other = await h.send("What is the weather like today")
    assert other[0]["chat_id"] == current and other[0]["data"]["routed"] == "new_task"
    assert h.store.interaction(row["interaction_id"])["state"] == "pending"
    assert h.store.task(side_task)["state"] == "needs_input"
    assert not h.store.device("sim-device-01")["voice_window_until"], "a follow-up that is no answer ends the window"


async def test_question_from_an_api_message_is_carded_when_idle_with_its_speech_at_once(make):
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Temperature in one of my rooms", origin="api")
    assert first[-1]["data"]["reason"] == "question" and "Which room?" in spoken(first)
    task_id = first[0]["task_id"]
    await h.wait_for(lambda: seen, timeout=5, what="the ask result")
    await h.wait_task(task_id, "needs_input")
    row = question_row(h, task_id)
    cards = h.deliveries(slot=f"int:{row['interaction_id']}")
    assert [(d["tier"], d["speak"]) for d in cards] == [("WHEN_IDLE", "Which room?")], \
        "one WHEN_IDLE card with its speech, no second one after the window"
    assert row["spoken_at"] is None and not h.store.device("sim-device-01")["voice_window_until"]
    await h.idle(first[0]["chat_id"])
    later = [d for d in h.deliveries(task_id=task_id) if d["gseq"] > cards[0]["gseq"]]
    assert not [d for d in later if json.loads(d["payload_json"])["state"] == "progress"], \
        "no working item replaces the open card"


async def test_question_in_the_devices_voice_reply_is_carded_without_speech_then_spoken_after_the_window(make):
    seen = []
    h = await make(asking(seen))
    _, first = await h.send("Temperature in one of my rooms")
    task_id = first[0]["task_id"]
    await h.wait_for(lambda: seen, timeout=5, what="the ask result")
    row = question_row(h, task_id)
    cards = h.deliveries(slot=f"int:{row['interaction_id']}")
    assert [(d["tier"], d["speak"]) for d in cards] == [("SILENT", None), ("WHEN_IDLE", "Which room?")]
