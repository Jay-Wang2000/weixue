"""ADR-9 inside the service: budget → pending, ownership by our own token, decisions (route B), single replay,
dedupe, dispositions (unknown blocks an identical write)."""
import asyncio
import json
import uuid

from fake_hamcp import FakeHaMcp, Pending
from weixue_agent import ha_adapter

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


async def owned(h, row):
    """The original request's wait elapses: the token it returns makes the hold ours."""
    h.fake_ha.elapse_wait()
    await h.wait_for(lambda: h.store.approval(row["external_ref"])["state"] == "awaiting_decision_after_wait",
                     what="wait elapsed")
    await h.wait_for(lambda: h.store.interaction(row["interaction_id"])["state"] == "pending", what="card actionable")
    return h.store.approval(row["external_ref"])


async def test_held_call_is_read_only_until_its_own_request_returns_the_token(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    assert results["first"][0].startswith("PENDING_APPROVAL id=apr_")
    token = next(iter(h.fake_ha.queue))
    assert token not in results["first"][0]
    assert row["state"] == "uncorrelated" and row["voice_can_approve"] == 0
    card = json.loads(h.deliveries(slot=f"int:{row['interaction_id']}")[-1]["payload_json"])
    assert "options" not in card
    await h.poll()
    assert h.store.approval(row["external_ref"])["ha_token"] is None
    resp = await decide(h, events[0]["chat_id"], row["interaction_id"], "deny")
    assert resp.status_code == 409 and h.fake_ha.decisions == []
    approval = await owned(h, row)
    assert approval["ha_token"] == token and approval["token_source"] == "response"
    row = h.store.interaction(row["interaction_id"])
    assert row["voice_can_approve"] == 1
    action = json.loads(row["action_json"])
    assert action["text"] == "switch.turn_on switch.working_desk_plug" and action["reversible"] is True
    spoken = h.deliveries(slot=f"int:{row['interaction_id']}")[-1]
    assert json.loads(spoken["payload_json"])["options"] and spoken["speak"]
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


async def test_panel_approval_lets_the_original_request_execute(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    h.fake_ha.panel(next(iter(h.fake_ha.queue)), "approved")
    task = await h.wait_task(task_id, "done")
    assert task["outcome"] == "ok"
    assert [c["tool"] for c in h.fake_ha.calls] == ["ha_call_service"] and h.fake_ha.decisions == []
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


async def test_concurrent_decisions_claim_a_single_replay(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    await owned(h, row)
    gw = h.service.ha
    outcomes = await asyncio.gather(gw.decide(row["external_ref"], "approve", "tap", None),
                                    gw.decide(row["external_ref"], "approve", "panel", None), return_exceptions=True)
    assert sum(1 for o in outcomes if isinstance(o, ha_adapter.AlreadyDecided)) <= 1
    await h.wait_task(task_id, "done")
    assert len(h.fake_ha.executed) == 1 and len(h.fake_ha.calls) == 2


async def test_wait_elapsed_answer_after_a_recorded_approval_still_replays(make):
    """Whichever of the decision and the original's wait-elapsed answer is recorded second claims the replay."""
    h = await make()
    gw = h.service.ha
    now = ha_adapter.timeutil.iso()
    token = "tok-" + "x" * 20
    pending = Pending("ha_call_service", dict(PLUG))
    pending.token, pending.decision, pending.timed_out = token, "approved", True
    h.fake_ha.queue[token] = pending
    h.store.insert("approvals", {
        "approval_id": "apr_race", "device_id": "sim-device-01", "tool": "ha_call_service",
        "args_json": json.dumps(PLUG), "args_hash": ha_adapter.args_hash(PLUG), "state": "pending",
        "ha_token": token, "token_source": "response", "decision": "approve", "via": "tap", "decided_at": now,
        "waiters_json": "[]", "created_at": now, "updated_at": now})
    gw._awaiting("apr_race", token, 60)
    await h.wait_for(lambda: h.store.approval("apr_race")["state"] == "executed", what="replay")
    assert len(h.fake_ha.executed) == 1
    assert not gw._claim_replay("apr_race")


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
    await owned(h, row)
    await decide(h, events[0]["chat_id"], row["interaction_id"], "deny")
    await h.wait_task(task_id, "done")
    await h.wait_task(events_b[0]["task_id"], "done")
    assert len(results["events"]) == 2


async def test_another_clients_identical_call_is_never_adopted(make):
    """F01: a single tool/args/time match in the pending listing is no ownership, even when ours is not listed."""
    results = {}
    fake = FakeHaMcp(wait_seconds=30)
    h = await make(held_script(results), fake_ha=fake)
    stranger = Pending("ha_call_service", dict(PLUG))
    fake.queue[stranger.token] = stranger
    fake.call_delay = 5.0
    _, events = await h.send("turn on the desk plug")
    await h.wait_task(events[0]["task_id"], "needs_approval")
    for _ in range(3):
        await h.poll()
    row = h.store.one("SELECT * FROM interactions WHERE task_id = ?", (events[0]["task_id"],))
    assert row["state"] == "uncorrelated"
    assert h.store.approval(row["external_ref"])["ha_token"] is None
    resp = await decide(h, events[0]["chat_id"], row["interaction_id"], "approve")
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "invalid_state"
    resp = await h.http.post(f"/v1/chats/{events[0]['chat_id']}/interrupt",
                             json={"decision_id": str(uuid.uuid4()), "reason": "panel"})
    assert resp.status_code == 200
    await h.restart()
    assert h.fake_ha.decisions == [] and stranger.token in h.fake_ha.queue


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
    token = (await owned(h, row))["ha_token"]
    resp = await h.http.post(f"/v1/chats/{events[0]['chat_id']}/interrupt",
                             json={"decision_id": str(uuid.uuid4()), "reason": "panel"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["task"]["state"] == "cancelled"
    assert {"token": token, "decision": "denied"} in h.fake_ha.decisions


async def test_boot_denies_only_provably_owned_pending_calls(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    token = (await owned(h, row))["ha_token"]
    stranger = Pending("ha_call_service", {"domain": "lock", "service": "unlock", "entity_id": "lock.front"})
    h.fake_ha.queue[stranger.token] = stranger
    await h.restart()
    decided = {d["token"]: d["decision"] for d in h.fake_ha.decisions}
    assert decided.get(token) == "denied" and stranger.token not in decided
    assert h.store.approval(row["external_ref"])["state"] == "denied"


async def test_transport_failure_before_the_budget_is_unknown_and_blocks_an_identical_write(make):
    """F02: unknown is its own disposition in tool_calls and the task, and no identical write is sent again."""
    results = {}
    h = await make(held_script(results, args=LIGHT))
    h.transport.drop.add("/private_test")
    _, events = await h.send("turn on the living room light")
    task = await h.wait_task(events[0]["task_id"], "done")
    h.transport.drop.clear()
    call = h.store.one("SELECT * FROM tool_calls WHERE task_id = ? AND tool = 'ha_call_service'", (task["task_id"],))
    assert call["status"] == "unknown" and call["approval_id"]
    assert task["outcome"] == "unknown" and task["retry_allowed"] == 0
    assert json.loads(task["side_effects_json"]) == [
        {"tool": "ha_call_service", "target": "light.living_room_ceiling_lights", "status": "unknown"}]
    assert len(h.fake_ha.calls) == 1
    _, again = await h.send("turn on the living room light again")
    second = await h.wait_task(again[0]["task_id"], "done")
    assert results["first"][-1] == ha_adapter.BLOCKED_TEXT
    assert len(h.fake_ha.calls) == 1
    blocked = h.store.one("SELECT * FROM tool_calls WHERE task_id = ? AND tool = 'ha_call_service'", (second["task_id"],))
    assert blocked["status"] == "not_run" and second["outcome"] != "ok"


async def test_unknown_read_is_not_blocked(make):
    async def script(turn):
        await turn.tool("ha_get_state", {"entity_id": "sun.sun"})
        await turn.text("Checked.")
    h = await make(script)
    h.transport.drop.add("/private_test")
    _, events = await h.send("is the sun up")
    await h.wait_task(events[0]["task_id"], "done")
    h.transport.drop.clear()
    _, again = await h.send("is the sun up now")
    await h.wait_task(again[0]["task_id"], "done")
    assert len(h.fake_ha.calls) == 2


async def test_not_sent_failure_is_not_run_and_allows_a_retry(make):
    results = {}
    h = await make(held_script(results, args=LIGHT))
    h.transport.refuse.add("/private_test")
    _, events = await h.send("turn on the living room light")
    task = await h.wait_task(events[0]["task_id"], "done")
    h.transport.refuse.clear()
    call = h.store.one("SELECT * FROM tool_calls WHERE task_id = ? AND tool = 'ha_call_service'", (task["task_id"],))
    assert call["status"] == "not_run" and h.store.approval(call["approval_id"])["state"] == "not_sent"
    assert task["outcome"] == "none" and task["retry_allowed"] == 1
    _, again = await h.send("turn on the living room light again")
    await h.wait_task(again[0]["task_id"], "done")
    assert h.fake_ha.states["light.living_room_ceiling_lights"] == "on"


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


async def test_a_token_arriving_after_cancel_is_denied_not_offered(make):
    results = {}
    h = await make(held_script(results), fake_ha=FakeHaMcp(wait_seconds=30))
    events, task_id, row = await held(h)
    resp = await h.http.post(f"/v1/chats/{events[0]['chat_id']}/interrupt",
                             json={"decision_id": str(uuid.uuid4()), "reason": "panel"})
    assert resp.status_code == 200 and h.fake_ha.decisions == []
    token = next(iter(h.fake_ha.queue))
    h.fake_ha.elapse_wait()
    await h.wait_for(lambda: h.fake_ha.decisions, what="deny after the token arrived")
    assert h.fake_ha.decisions == [{"token": token, "decision": "denied"}]
    assert h.store.approval(row["external_ref"])["state"] == "denied"
    cards = [json.loads(d["payload_json"]) for d in h.deliveries(slot=f"int:{row['interaction_id']}")]
    assert all("options" not in c for c in cards)
