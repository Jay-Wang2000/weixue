"""ADR-9 inside the service: budget → pending, exact ownership, decisions (route B), replay, dedupe."""
import asyncio
import json
import uuid

import pytest

from fake_hamcp import FakeHaMcp, Pending
from weixue_agent import ha_adapter, interactions

PLUG = {"domain": "switch", "service": "turn_on", "entity_id": "switch.working_desk_plug"}
LIGHT = {"domain": "light", "service": "turn_on", "entity_id": "light.living_room_ceiling_lights"}


def held_script(results, args=PLUG):
    async def script(turn):
        if "[event approval" in turn.prompt and turn.user_text.startswith("[event approval"):
            results.setdefault("events", []).append(turn.prompt)
            line = turn.user_text
            await turn.text("It's on now." if "executed" in line else "Okay, I didn't do it.")
            return
        await turn.text("Turning it on.")
        results.setdefault("first", []).append(await turn.tool("ha_call_service", args))
        await turn.text("I need your OK on the panel.")
    return script


async def held(h, text="turn on the desk plug"):
    _, events = await h.send(text)
    task_id = events[0]["task_id"]
    await h.wait_task(task_id, "needs_approval")
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ? AND source = 'ha_mcp'", (task_id,))
    return events, task_id, row


async def decide(h, chat_id, interaction_id, decision, via="panel"):
    return await h.http.post(f"/v1/chats/{chat_id}/{decision}",
                             json={"interaction_id": interaction_id, "decision_id": str(uuid.uuid4()), "via": via})


@pytest.fixture(autouse=True)
def quick_poll(monkeypatch):
    monkeypatch.setattr(interactions, "HA_PENDING_POLL_S", 0.05)


async def test_free_light_call_runs_inside_the_budget(make):
    results = {}
    h = await make(held_script(results, args=LIGHT))
    _, events = await h.send("turn on the living room light")
    task = await h.wait_task(events[0]["task_id"], "done")
    assert json.loads(results["first"][0]) == {"success": True}
    assert h.fake_ha.states["light.living_room_ceiling_lights"] == "on"
    assert json.loads(task["side_effects_json"]) == [
        {"tool": "ha_call_service", "target": "light.living_room_ceiling_lights", "status": "confirmed"}]
    assert not h.events(events[0]["chat_id"], "needs_approval")


async def test_held_call_pending_then_denied_through_the_policy_api(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    assert results["first"][0].startswith("PENDING_APPROVAL id=apr_")
    token = next(iter(h.fake_ha.queue))
    assert token not in results["first"][0]
    assert row["voice_can_approve"] == 1
    action = json.loads(row["action_json"])
    assert action["text"] == "switch.turn_on switch.working_desk_plug" and action["reversible"] is True
    await h.poll()
    approval = h.store.approval(row["external_ref"])
    assert approval["ha_token"] == token and approval["state"] == "pending"
    resp = await decide(h, events[0]["chat_id"], row["interaction_id"], "deny")
    assert resp.status_code == 200, resp.text
    assert h.fake_ha.decisions == [{"token": token, "decision": "denied"}]
    task = await h.wait_task(task_id, "done")
    assert task["outcome"] == "denied"
    assert json.loads(task["side_effects_json"])[0]["status"] == "not_run"
    assert "switch.working_desk_plug" not in [e["args"].get("entity_id") for e in h.fake_ha.executed]
    assert "Already handled; do not call the tool again." in results["events"][0]
    resolved = h.events(events[0]["chat_id"], "resolved")
    assert resolved[0]["data"]["state"] == "denied"


async def test_early_approve_lets_the_original_request_execute(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    await h.poll()
    resp = await decide(h, events[0]["chat_id"], row["interaction_id"], "approve")
    assert resp.status_code == 200, resp.text
    task = await h.wait_task(task_id, "done")
    assert task["outcome"] == "ok"
    assert h.fake_ha.states.get("switch.working_desk_plug") is None
    assert [c["tool"] for c in h.fake_ha.calls] == ["ha_call_service"]
    assert h.store.approval(row["external_ref"])["state"] == "executed"
    assert "executed" in results["events"][0]


async def test_late_approve_replays_once_after_our_approve_succeeded(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=0.4))
    events, task_id, row = await held(h)
    await h.wait_for(lambda: h.store.approval(row["external_ref"])["state"] == "awaiting_decision_after_wait",
                     what="wait elapsed")
    assert h.store.approval(row["external_ref"])["ha_token"]
    resp = await decide(h, events[0]["chat_id"], row["interaction_id"], "approve")
    assert resp.status_code == 200, resp.text
    await h.wait_task(task_id, "done")
    assert h.store.approval(row["external_ref"])["state"] == "executed"
    assert len(h.fake_ha.calls) == 2 and len(h.fake_ha.executed) == 1


async def test_lost_approve_response_is_unknown_and_never_replayed(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=0.4))
    events, task_id, row = await held(h)
    await h.wait_for(lambda: h.store.approval(row["external_ref"])["state"] == "awaiting_decision_after_wait",
                     what="wait elapsed")
    h.transport.drop.add("/api/policy/approve")
    resp = await decide(h, events[0]["chat_id"], row["interaction_id"], "approve")
    assert resp.status_code == 200, resp.text
    assert h.store.approval(row["external_ref"])["state"] == "unknown"
    await h.wait_task(task_id, "done")
    assert len(h.fake_ha.calls) == 1
    task = h.store.task(task_id)
    assert task["outcome"] == "unknown"


async def test_unknown_token_at_decision_time_is_expired(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=0.4))
    events, task_id, row = await held(h)
    await h.wait_for(lambda: h.store.approval(row["external_ref"])["state"] == "awaiting_decision_after_wait",
                     what="wait elapsed")
    h.fake_ha.queue.clear()
    resp = await decide(h, events[0]["chat_id"], row["interaction_id"], "approve")
    assert resp.status_code == 200
    assert h.store.approval(row["external_ref"])["state"] == "expired"
    task = await h.wait_task(task_id, "done")
    assert task["outcome"] == "expired"


async def test_identical_call_joins_the_pending_one_without_a_second_request(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    other = await h.http.post("/v1/chats", json={"device_id": "sim-device-01"}, headers={"Idempotency-Key": "c2"})
    chat_b = other.json()["chat_id"]
    _, events_b = await h.send("turn on the desk plug too", chat=chat_b)
    await h.wait_task(events_b[0]["task_id"], "needs_approval")
    assert len(h.fake_ha.calls) == 1
    approval = h.store.approval(row["external_ref"])
    assert len(json.loads(approval["waiters_json"])) == 2
    await h.poll()
    await decide(h, events[0]["chat_id"], row["interaction_id"], "deny")
    await h.wait_task(task_id, "done")
    await h.wait_task(events_b[0]["task_id"], "done")
    assert len(results["events"]) == 2


async def test_another_clients_identical_call_makes_ownership_ambiguous(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    _, events = await h.send("turn on the desk plug")
    await h.wait_for(lambda: h.fake_ha.queue, what="held")
    stranger = Pending("ha_call_service", dict(PLUG))
    h.fake_ha.queue[stranger.token] = stranger
    await h.wait_task(events[0]["task_id"], "needs_approval")
    await h.poll()
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (events[0]["task_id"],))
    assert h.store.approval(row["external_ref"])["ha_token"] is None
    resp = await decide(h, events[0]["chat_id"], row["interaction_id"], "approve")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "invalid_state"
    assert h.fake_ha.decisions == []
    await asyncio.sleep(0.2)
    await h.poll()
    assert h.store.interaction(row["interaction_id"])["state"] == "uncorrelated"


async def test_truncated_listing_never_matches_a_long_argument(make):
    results = {}
    long_args = {"domain": "script", "service": "turn_on", "entity_id": "script.x", "data": {"note": "y" * 900}}
    h = await make(held_script(results, args=long_args), fake_ha=FakeHaMcp(wait_seconds=30))
    _, events = await h.send("run the script")
    await h.wait_task(events[0]["task_id"], "needs_approval")
    await h.poll()
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (events[0]["task_id"],))
    assert h.store.approval(row["external_ref"])["ha_token"] is None


async def test_cancel_denies_owned_tokens(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    await h.poll()
    token = h.store.approval(row["external_ref"])["ha_token"]
    resp = await h.http.post(f"/v1/chats/{events[0]['chat_id']}/interrupt",
                             json={"decision_id": str(uuid.uuid4()), "reason": "panel"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["state"] == "cancelled"
    assert {"token": token, "decision": "denied"} in h.fake_ha.decisions


async def test_boot_denies_only_provably_owned_pending_calls(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    await h.poll()
    token = h.store.approval(row["external_ref"])["ha_token"]
    stranger = Pending("ha_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"})
    h.fake_ha.queue[stranger.token] = stranger
    await h.restart()
    decided = {d["token"]: d["decision"] for d in h.fake_ha.decisions}
    assert decided.get(token) == "denied" and stranger.token not in decided
    assert h.store.approval(row["external_ref"])["state"] == "denied"


async def test_e2e_guard_refuses_free_writes_except_the_test_entity(make):
    results = {}
    config = {"alias": "Sunset lights", "triggers": [{"trigger": "sun", "event": "sunset"}],
              "actions": [{"action": "light.turn_on", "target": {"entity_id": "light.living_room_ceiling_lights"}}]}

    async def script(turn):
        results["auto"] = await turn.tool("ha_config_set_automation", {"config": config})
        results["light"] = await turn.tool("ha_call_service", LIGHT)
        await turn.text("Done.")
    h = await make(script)
    (h.settings.data_dir / "e2e-guard").touch()
    _, events = await h.send("make the automation")
    await h.wait_task(events[0]["task_id"], "done")
    assert results["auto"] == ha_adapter.GUARD_TEXT
    assert [c["tool"] for c in h.fake_ha.calls] == ["ha_call_service"]


async def test_tunnel_interface_missing_fails_closed(make):
    results = {}
    h = await make(held_script(results, args=LIGHT))
    h.service.ha.require_iface = "wg0"
    h.service.ha._iface_present = lambda name: False
    _, events = await h.send("turn on the light")
    await h.wait_task(events[0]["task_id"], "done")
    assert results["first"][0] == ha_adapter.UNREACHABLE_TEXT
    assert h.fake_ha.calls == []


async def test_health_needs_a_successful_ping(make):
    h = await make()
    assert not h.service.monitor.ha_healthy()
    await h.service.monitor.probe_all()
    assert h.service.monitor.ha_healthy()
    h.fake_ha.down = True
    assert not await h.service.ha.ping()


async def test_every_ha_mcp_tool_is_declared_and_only_the_hot_set_is_loaded(make):
    h = await make()
    _, events = await h.send("hello")
    await h.wait_task(events[0]["task_id"], "done")
    tools = {t["name"]: t for t in h.api.requests[0]["tools"]}
    assert {"ha_search", "ha_get_state", "ha_call_service", "ha_config_set_automation"} <= set(tools)
    assert tools["ha_config_set_automation"]["defer_loading"] is True
    assert "defer_loading" not in tools["ha_call_service"] and "defer_loading" not in tools["ha_search"]
    assert tools["tool_search_tool_bm25"]["type"] == "tool_search_tool_bm25_20251119"
    assert "memory" not in tools
    assert {"ask_user", "start_task", "set_chat_title", "notify_user", "set_result_tier", "awaiting_answer"} <= set(tools)
