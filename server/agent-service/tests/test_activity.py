"""Activity label of a working task (decision 68): mapping, rate limit, clearing, deliveries."""
import asyncio

import pytest

from conftest import DEVICE
from weixue_agent import activity, turns
from weixue_agent.store import loads
from weixue_agent.turns import TurnRun

READS = {"ha_search", "ha_get_state", "ha_get_history", "ha_get_overview", "ha_config_get_calendar_events"}


@pytest.fixture(autouse=True)
def short_interval(monkeypatch):
    monkeypatch.setattr(turns, "ACTIVITY_MIN_INTERVAL_S", 0.3)


@pytest.mark.parametrize("tool,server,kind", [
    ("web_search", True, activity.SEARCH),
    ("web_fetch", True, activity.SEARCH),
    ("code_execution", True, activity.SEARCH),
    ("bash_code_execution", True, activity.SEARCH),
    ("tool_search_tool_bm25", True, activity.FIND_TOOL),
    ("ha_get_state", False, activity.HOUSE),
    ("ha_list_floors_areas", False, activity.HOUSE),
    ("ha_search", False, activity.HOUSE),
    ("ha_config_get_calendar_events", False, activity.HOUSE),
    ("ha_call_service", False, activity.ACT),
    ("ha_config_set_automation", False, activity.ACT),
    ("memory", False, activity.THINK),
    ("start_task", False, activity.THINK),
    ("device_action", False, activity.THINK),
    ("some_server_tool", True, activity.THINK),
    (None, False, activity.THINK),
])
def test_kind_of_tool(tool, server, kind):
    assert activity.kind_of(tool, server, READS.__contains__) == kind


def test_labels_follow_the_language():
    assert activity.label(activity.SEARCH, "en") == "Searching…"
    assert activity.label(activity.SEARCH, "zh") == "搜索中…"
    assert activity.label(activity.HOUSE, "zh") == "查看家里状态…"
    assert activity.label(activity.ACT, "en") == "Acting…"
    assert activity.label(activity.FIND_TOOL, "zh") == "找工具…"
    assert activity.label(activity.THINK, "zh") == "思考中…"
    assert all(len(text) <= activity.MAX_CHARS for kinds in activity.LABELS.values() for text in kinds.values())


def activities(h, chat_id):
    return [e["data"].get("activity") for e in h.events(chat_id, "progress")]


async def test_each_tool_sets_the_activity_of_the_turn(make):
    async def script(turn):
        await asyncio.sleep(0.5)
        turn.server_tool("web_search", "news")
        await asyncio.sleep(0.5)
        await turn.tool("ha_get_state", {"entity_id": "light.kitchen"})
        await asyncio.sleep(0.5)
        await turn.tool("ha_call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen"})
        await asyncio.sleep(0.5)
        await turn.text("Done.")
    h = await make(script)
    sent = await h.send_json("check the news and the kitchen light")
    await h.wait_task(sent["task_id"], "done")
    seen = [a for i, a in enumerate(activities(h, sent["chat_id"])) if i == 0 or a != activities(h, sent["chat_id"])[i - 1]]
    assert seen == ["Thinking…", "Searching…", "Checking the house…", "Acting…"]
    payloads = [loads(d["payload_json"]) for d in h.deliveries(kind="agent", task_id=sent["task_id"])]
    shown = [p["progress"].get("activity") for p in payloads if p.get("progress")]
    assert shown and set(shown) <= {"Searching…", "Checking the house…", "Acting…"}
    assert h.engine.activities == {}


async def test_chinese_turn_gets_chinese_labels(make):
    async def script(turn):
        await asyncio.sleep(0.5)
        turn.server_tool("web_search", "爱尔兰新闻")
        await turn.text("好的。")
    h = await make(script)
    sent = await h.send_json("今天爱尔兰有什么新闻")
    await h.wait_task(sent["task_id"], "done")
    assert "搜索中…" in activities(h, sent["chat_id"])
    assert activities(h, sent["chat_id"])[0] == "思考中…"


async def test_changes_are_rate_limited_and_the_latest_wins(make):
    h = await make()
    sent = await h.send_json("hello")
    await h.wait_task(sent["task_id"], "done")
    h.store.update("tasks", "task_id", sent["task_id"], {"state": "progress"})
    run = TurnRun(turn_id="trn_x", task_id=sent["task_id"], chat_id=sent["chat_id"], device_id=DEVICE,
                  trace_id="0" * 32, kind="user", lang="en")
    before = len(activities(h, sent["chat_id"]))
    h.engine.set_activity(run, activity.SEARCH)
    h.engine.set_activity(run, activity.HOUSE)
    h.engine.set_activity(run, activity.ACT)
    assert activities(h, sent["chat_id"])[before:] == ["Searching…"]
    await asyncio.sleep(0.4)
    assert activities(h, sent["chat_id"])[before:] == ["Searching…", "Acting…"]
    h.engine.set_activity(run, activity.ACT)
    await asyncio.sleep(0.4)
    assert activities(h, sent["chat_id"])[before:] == ["Searching…", "Acting…"]


async def test_turn_end_clears_the_activity_of_a_task_still_working(make):
    h = await make()
    sent = await h.send_json("hello")
    await h.wait_task(sent["task_id"], "done")
    h.store.update("tasks", "task_id", sent["task_id"], {"state": "progress"})
    run = TurnRun(turn_id="trn_x", task_id=sent["task_id"], chat_id=sent["chat_id"], device_id=DEVICE,
                  trace_id="0" * 32, kind="user", lang="zh")
    other = TurnRun(turn_id="trn_y", task_id=sent["task_id"], chat_id=sent["chat_id"], device_id=DEVICE,
                    trace_id="0" * 32, kind="user", lang="zh")
    h.engine.set_activity(run, activity.SEARCH)
    assert loads(h.store.task(sent["task_id"])["progress_json"])["activity"] == "搜索中…"
    h.engine.set_activity(other, None)
    await asyncio.sleep(0.4)
    assert activities(h, sent["chat_id"])[-1] == "搜索中…"
    h.engine.set_activity(run, None)
    await asyncio.sleep(0.4)
    events = h.events(sent["chat_id"], "progress")
    assert "activity" not in events[-1]["data"]
    assert "activity" not in loads(h.store.task(sent["task_id"])["progress_json"])
    last = loads(h.deliveries(kind="agent", task_id=sent["task_id"])[-1]["payload_json"])
    assert last["state"] == "progress" and "activity" not in last["progress"]


async def test_no_working_item_while_a_card_is_open(make):
    h = await make()
    sent = await h.send_json("hello")
    await h.wait_task(sent["task_id"], "done")
    h.store.update("tasks", "task_id", sent["task_id"], {"state": "progress"})
    run = TurnRun(turn_id="trn_x", task_id=sent["task_id"], chat_id=sent["chat_id"], device_id=DEVICE,
                  trace_id="0" * 32, kind="user", lang="en")
    h.engine.set_activity(run, activity.SEARCH)
    count = len(h.deliveries(kind="agent", task_id=sent["task_id"]))
    h.engine.set_activity(run, activity.THINK)
    h.store.update("tasks", "task_id", sent["task_id"], {"state": "needs_input"})
    await asyncio.sleep(0.4)
    assert len(h.deliveries(kind="agent", task_id=sent["task_id"])) == count
    assert activities(h, sent["chat_id"])[-1] == "Searching…"


async def test_reply_end_mid_turn_sends_the_current_activity(make):
    async def script(turn):
        await turn.text("Let me look.")
        await asyncio.sleep(0.5)
        turn.server_tool("web_search", "news")
        await asyncio.sleep(1.0)
        await turn.text("Here it is.")
    h = await make(script)
    _, events = await h.send("what's the news")
    task_id = events[0]["task_id"]
    assert any(e["type"] == "reply_end" and e["data"]["reason"] == "hold_elapsed" for e in events)
    await h.wait_task(task_id, "done")
    working = [loads(d["payload_json"]) for d in h.deliveries(kind="agent", task_id=task_id)]
    assert [p["progress"].get("activity") for p in working if p["state"] == "progress"] == ["Searching…"]
