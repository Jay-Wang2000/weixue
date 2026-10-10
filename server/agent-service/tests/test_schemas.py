"""Committed schemas match the code; live events and tasks validate against them."""
import json
from pathlib import Path

import jsonschema
from referencing import Registry, Resource

from tools.export_schemas import OUT, render
from weixue_agent.schemas import all_schemas


def registry():
    resources = []
    for name, schema in all_schemas().items():
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


def validator(name):
    schema = all_schemas()[name]
    return jsonschema.Draft202012Validator(schema, registry=registry())


def test_committed_schemas_have_no_drift():
    expected = {name: render(schema) for name, schema in all_schemas().items()}
    actual = {p.name: p.read_text() for p in Path(OUT).glob("*.schema.json")}
    assert actual == expected


async def test_events_tasks_and_interactions_validate(make):
    async def script(turn):
        await turn.text("Checking.")
        await turn.tool("ha_call_service", {"domain": "switch", "service": "turn_on", "entity_id": "switch.x"})
        await turn.text("Waiting for you.")
    h = await make(script)
    _, events = await h.send("plug on")
    await h.wait_task(events[0]["task_id"], "needs_approval")
    event_v = validator("event.schema.json")
    for event in h.events(events[0]["chat_id"]):
        event_v.validate(event)
    task = (await h.http.get(f"/v1/tasks/{events[0]['task_id']}")).json()
    validator("task.schema.json").validate(task)
    assert task["pending"] and task["pending"][0]["kind"] == "approval"
    resp = await h.http.post("/v1/chats/current/messages", json={"device_id": "sim-device-01", "text": "x"},
                             headers={"Idempotency-Key": "k-schema"})
    validator("message-response.schema.json").validate(resp.json())


def test_request_bodies_reject_extra_fields_and_bad_ids():
    from pydantic import ValidationError
    from weixue_agent.schemas import Answer, MessageBody
    import pytest
    with pytest.raises(ValidationError):
        MessageBody.model_validate({"device_id": "d", "text": "x", "extra": 1})
    with pytest.raises(ValidationError):
        Answer.model_validate({"interaction_id": "int_bad", "decision_id": "00000000-0000-0000-0000-000000000000",
                               "text": "x", "via": "panel"})
    with pytest.raises(ValidationError):
        Answer.model_validate({"interaction_id": "int_" + "0" * 26, "decision_id": "00000000-0000-0000-0000-000000000000",
                               "text": "x", "option_ids": ["a"], "via": "panel"})
