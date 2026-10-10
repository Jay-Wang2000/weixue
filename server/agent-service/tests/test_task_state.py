"""Task lifecycle and error mapping (ADR-11)."""
import json

from fake_api import error_response
from weixue_agent.turns import Engine


def test_outcomes():
    assert Engine.outcome_of("done", []) == "ok"
    assert Engine.outcome_of("done", [{"status": "unknown"}], "denied") == "unknown"
    assert Engine.outcome_of("cancelled", [{"status": "confirmed"}]) == "partial"
    assert Engine.outcome_of("failed", []) == "none"


async def test_refusal_fails_the_task_with_a_spoken_cue_and_appends_nothing(make):
    async def script(turn):
        await turn.text("Partial words")
        turn.stop_reason = "refusal"
    h = await make(script)
    _, events = await h.send("something refused")
    spoken = "".join(e["data"]["delta"] for e in events if e["type"] == "text")
    assert spoken.endswith("Sorry, I can't help with that one.") and events[-1]["data"]["reason"] == "turn_done"
    task = await h.wait_task(events[0]["task_id"], "failed")
    assert json.loads(task["error_json"])["code"] == "refusal"
    assert h.events(events[0]["chat_id"], "failed")[0]["data"]["speak"] is None
    rows = h.transcript(events[0]["chat_id"]).rows()
    assert [r["role"] for r in rows] == ["user", "system"] and all(r["state"] == "tail" for r in rows)


async def test_api_errors_map_to_codes_and_spoken_cues(make):
    h = await make()
    cases = [(429, "rate_limit_error", "rate_limit", "rate limited"),
             (401, "authentication_error", "auth", "isn't authorised"),
             (529, "overloaded_error", "api_error", "something went wrong"),
             (400, "invalid_request_error", "billing", "billing problem")]
    for status, kind, code, cue in cases:
        message = "Your credit balance is too low" if code == "billing" else "error"
        h.api.fail_next = error_response(status, message, kind)
        _, events = await h.send(f"case {status}")
        task = await h.wait_task(events[0]["task_id"], "failed")
        assert json.loads(task["error_json"])["code"] == code
        assert cue in "".join(e["data"]["delta"] for e in events if e["type"] == "text")
        assert events[-1]["type"] == "reply_end"


async def test_retention_400_on_fable_switches_the_task_model_and_retries_once(make):
    async def script(turn):
        await turn.text("On it.")
        await turn.tool("start_task", {"title": "Research"})
        await turn.text(f"Done on {turn.model}.")
    h = await make(script)
    h.api.reject_models["claude-fable-5-1"] = lambda: error_response(
        400, "In order to access this model, your organization or workspace must have data retention enabled.")
    _, events = await h.send("Research something")
    task = await h.wait_task(events[0]["task_id"], "done")
    assert h.store.chat(task["chat_id"])["task_model"] == "claude-opus-5-5"
    assert h.api.requests[-1]["model"] == "claude-opus-5-5"
    assert h.events(task["chat_id"], "done")[0]["data"]["summary"] == "Done on claude-opus-5-5."
    assert h.store.one("SELECT code FROM errors WHERE code = 'retention'")


async def test_transitions_and_usage_are_recorded(make):
    h = await make()
    first = await h.send_json("hello")
    await h.wait_task(first["task_id"], "done")
    rows = h.store.all("SELECT from_state, to_state FROM task_transitions WHERE task_id = ? ORDER BY id",
                       (first["task_id"],))
    assert [(r["from_state"], r["to_state"]) for r in rows] == [(None, "progress"), ("progress", "done")]
    trace = (await h.http.get(f"/v1/tasks/{first['task_id']}/trace")).json()
    usage = trace["usage"][0]
    assert trace["metrics"]["turns"] == 1 and usage["model"] == "claude-sonnet-5-5"
    assert usage["mode"] == "quick" and usage["effort"] == "low" and usage["request_id"].startswith("req_")
    assert usage["ttft_ms"] is not None and usage["stop_reason"] == "end_turn"
    assert h.store.one("SELECT * FROM usage_daily")["results"] == 1
