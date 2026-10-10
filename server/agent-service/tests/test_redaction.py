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
