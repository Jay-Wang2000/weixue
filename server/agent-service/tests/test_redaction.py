"""Synthetic secrets never reach logs, traces, errors, MCP config, CLI env or monitor rows."""
import json

from conftest import HA_URL, TOKEN
from weixue_agent import jsonlog


async def test_secrets_absent_from_logs_trace_rows_and_cli_options(make, capsys):
    secret_path = HA_URL.rsplit("/", 1)[-1]
    jsonlog.register_secrets([TOKEN, HA_URL, secret_path, "sk-ant-test-key-0000000000"])

    async def script(turn):
        await turn.tool("ha_get_state", {"entity_id": "light.x", "api_token": "sk-ant-leak-000000000000",
                                                  "url": HA_URL})
        await turn.text("Done.")
    h = await make(script)
    _, events = await h.send(f"my token is {TOKEN}")
    await h.wait_task(events[0]["task_id"], "done")
    trace = (await h.http.get(f"/v1/tasks/{events[0]['task_id']}/trace")).text
    status = (await h.http.get("/v1/status")).text
    rows = json.dumps([h.store.all(f"SELECT * FROM {t}") for t in
                       ("tool_calls", "errors", "health_probes", "approvals", "usage", "connection_events")])
    jsonlog.log("probe", detail=f"calling {HA_URL} with token={TOKEN}")
    out = capsys.readouterr().out
    for blob in (trace, status, out):
        assert TOKEN not in blob and secret_path not in blob and "sk-ant-leak" not in blob
    assert "sk-ant-leak" not in rows and secret_path not in rows.replace(json.dumps(HA_URL)[1:-1], "")
    assert all(secret_path not in json.dumps([body["system"], body["tools"]]) for body in h.api.requests)


def test_redact_args_hides_sensitive_keys_and_caps_strings():
    out = jsonlog.redact_args({"pin": "1234", "nested": {"secret_key": "x"}, "text": "y" * 500})
    assert out["pin"] == "[redacted]" and out["nested"]["secret_key"] == "[redacted]"
    assert len(out["text"]) == jsonlog.ARG_STR_MAX


def test_request_parameters_match_the_brief():
    from weixue_agent import models
    kwargs = models.request_kwargs(models.MODELS[models.TASK], "system")
    assert kwargs["thinking"] == {"type": "adaptive", "display": "updates",
                                  "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
    assert kwargs["output_config"] == {"effort": "low"} and kwargs["fallbacks"] == "default"
    assert set(kwargs["betas"]) == {"server-side-fallback-2026-07-01", "mid-conversation-output-config-2026-07-01",
                                    "thinking-display-updates-2026-08-18", "thinking-binding-controls-2026-08-01",
                                    "compact-2026-09-04"}
    assert models.MODELS == {"quick": "claude-sonnet-5-5", "task": "claude-fable-5-1"}
    assert models.EFFORT == {"quick": "low", "task": "high"}
    assert [t["type"] for t in models.server_tools(models.MODELS[models.QUICK])] == [
        "tool_search_tool_bm25_20251119", "web_search_20260209", "web_fetch_20260209"]
    assert all(t.get("max_uses") == 5 for t in models.server_tools("x") if t["name"].startswith("web"))
    assert "user_location" not in json.dumps(models.server_tools("x"))


async def test_secrets_are_scrubbed_before_persistence_and_model_submission(make):
    """F08: user text and tool results are scrubbed before the transcript and the model see them; text derived
    from model output (titles, final text, events, deliveries, the trace) before it is stored. Committed
    assistant blocks are kept as the API sent them."""
    registered = "registered-secret-" + "q" * 16
    shaped = "sk-proj-" + "Z" * 32
    google = "AIza" + "G" * 35
    jsonlog.register_secret(registered)

    async def script(turn):
        await turn.text("Checking.")
        await turn.tool("ha_get_state", {"entity_id": "sensor.leaky"})
        await turn.tool("set_chat_title", {"title": f"Keys {google}"})
        await turn.text(f"Done; your key {google} is set.")
    h = await make(script)
    h.fake_ha.states["sensor.leaky"] = f"value {registered} and {shaped}"
    _, events = await h.send(f"remember {registered} and {shaped}")
    task_id = events[0]["task_id"]
    await h.wait_task(task_id, "done")
    trace = (await h.http.get(f"/v1/tasks/{task_id}/trace")).text
    tables = ("transcript", "turns", "tasks", "chats", "chat_events", "deliveries", "tool_calls", "approvals",
              "chat_notes", "interactions")
    rows = json.dumps([h.store.all(f"SELECT * FROM {t}") for t in tables])
    requests = json.dumps([body["messages"] for body in h.api.requests])
    for secret in (registered, shaped):
        assert secret not in rows and secret not in requests and secret not in trace
    derived = json.dumps([h.store.all(f"SELECT * FROM {t}") for t in ("turns", "tasks", "chats", "chat_events",
                                                                          "deliveries", "tool_calls")])
    assert google not in trace and google not in derived


def test_api_key_shapes_are_redacted():
    for value in ("sk-proj-" + "a" * 30, "AIza" + "b" * 35, "ghp_" + "c" * 36, "xoxb-1234567890-abc",
                  "AKIAABCDEFGHIJKLMNOP", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.sig_part",
                  "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----"):
        assert value not in jsonlog.redact_text(f"x {value} y"), value
    assert jsonlog.redact_text("the light is on") == "the light is on"


def test_pem_pattern_stays_inside_one_json_string():
    begin_only = "-----BEGIN PRIVATE KEY-----\nMIIEbody"
    for value in (begin_only, begin_only + ' "quoted" \\', "a\\-----BEGIN RSA PRIVATE KEY-----x\\"):
        line = json.dumps({"a": value, "b": "after"})
        parsed = json.loads(jsonlog.redact_text(line))
        assert parsed["b"] == "after" and "MIIE" not in parsed["a"]
    assert jsonlog.redact_text(begin_only + "\nmore") == "[redacted]"


async def test_json_columns_are_scrubbed_per_string_and_stay_valid(make):
    h = await make()
    box = h.service.outbox
    begin_only = "-----BEGIN PRIVATE KEY-----\nMIIEbody"
    did = box.enqueue("sim-device-01", "agent", "WHEN_IDLE", "s",
                      {"title": begin_only, "origin_task_id": "t_none", "after": "kept"}, speak="x")
    row = h.store.delivery(did)
    payload = json.loads(row["payload_json"])
    assert payload["title"] == "[redacted]" and payload["after"] == "kept"
    assert box.public(row)["payload"]["after"] == "kept"
    h.store.update("deliveries", "delivery_id", did, {"payload_json": json.dumps({"k": begin_only, "n": 1})})
    assert json.loads(h.store.delivery(did)["payload_json"]) == {"k": "[redacted]", "n": 1}


async def test_outbox_and_trace_survive_a_malformed_payload_row(make):
    async def script(turn):
        await turn.text("Done.")
    h = await make(script)
    _, events = await h.send("hello")
    task_id = events[0]["task_id"]
    await h.wait_task(task_id, "done")
    did = h.service.outbox.enqueue("sim-device-01", "agent", "SILENT", "p", {"origin_task_id": task_id})
    h.store.execute("UPDATE deliveries SET payload_json = ? WHERE delivery_id = ?",
                    ('{"title":"[redacted]', did))
    assert h.service.outbox.public(h.store.delivery(did))["payload"] == {}
    assert (await h.http.get(f"/v1/tasks/{task_id}/trace")).status_code == 200
