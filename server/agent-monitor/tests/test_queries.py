"""Queries and aggregations over the fixture database; the column allowlist."""
import json
import re
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import fixture_db as fx
from agent_monitor import queries

DUBLIN = ZoneInfo("Europe/Dublin")
FORBIDDEN_COLUMNS = ("ha_token", "args_json", "args_hash", "waiters_json", "result_text", "payload_json", "lease_id",
                     "content_json", "system_text", "ha_tools_json", "request_hash", "response_json", "body_hash",
                     "question_json", "action_json", "decision_json", "input_transformations_json", "speak",
                     "ha_token_id")
FORBIDDEN_TABLES = ("transcript", "chat_prompts", "idempotency", "decisions")


def test_long_task_shows_every_tool_call_turn_and_usage(db):
    page = queries.timeline(db)
    group = next(g for g in page["groups"] if g["task_id"] == fx.T_LONG)
    rows = db.query("SELECT tool_use_id FROM tool_calls WHERE task_id = ?", (fx.T_LONG,))
    assert group["counts"]["tool_calls"] == len(rows) == 7
    assert {c["kind"] for c in group["tool_calls"]} == {"ha", "server", "memory", "app"}
    assert [t["kind"] for t in group["turns"]] == ["user", "steer"] and group["counts"]["steers"] == 1
    sums = db.one("SELECT SUM(input_tokens) AS i, SUM(output_tokens) AS o, SUM(cache_read_tokens) AS r, "
                  "SUM(cache_creation_tokens) AS c, SUM(cost_usd) AS usd FROM usage WHERE task_id = ?", (fx.T_LONG,))
    usage = group["usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["cache_read_tokens"],
            usage["cache_creation_tokens"]) == (sums["i"], sums["o"], sums["r"], sums["c"])
    assert abs(usage["cost_usd"] - sums["usd"]) < 1e-9 and usage["requests"] == 3
    assert group["model"] == "claude-fable-5-1" and group["duration_ms"] == 6 * 60 * 1000
    assert group["side_effects"][0]["target"] == "automation.sunset"


def test_held_calls_carry_decision_source_and_time(db):
    group = queries.task_detail(db, fx.T_LONG)
    calls = {c["tool_use_id"]: c for c in group["tool_calls"]}
    assert (calls["tu_4"]["approval_decision"], calls["tu_4"]["decided_via"], calls["tu_4"]["decided_by_device"]) == \
        ("approve", "panel", fx.DEVICE)
    assert (calls["tu_5"]["approval_decision"], calls["tu_5"]["decided_via"]) == ("deny", "voice")
    assert calls["tu_2"]["args_summary"] == {"entity_id": "light.living_room_ceiling_lights"}
    assert [i["via"] for i in group["interactions"]] == ["panel", "voice"]
    assert [t["reason"] for t in group["transitions"]] == ["created", "held", "approved", "steer", "end_turn"]
    assert len(group["usage_rows"]) == 3


def test_deliveries_include_acks_and_cut_off(db):
    group = queries.task_detail(db, fx.T_LONG)
    cut = next(d for d in group["deliveries"] if d["delivery_id"] == "dlv_2")
    assert cut["playback"] == "cut_off" and cut["acks"] == [
        {"delivery_id": "dlv_2", "outcome": "cut_off", "reason": "barge-in", "cut_at_ms": 1800,
         "received_at": cut["acks"][0]["received_at"]}]
    assert "payload_json" not in cut and "lease_id" not in cut and "speak" not in cut


def test_user_text_is_clipped_to_120(db):
    group = queries.task_detail(db, fx.T_LONG)
    first = group["turns"][0]
    assert len(first["text"]) == queries.TEXT_MAX and first["text"].endswith("…")
    assert len(first["final_text"]) == queries.TEXT_MAX
    assert group["turns"][1]["text"] == "make it 20 minutes before sunset"


def test_cancel_and_steer_are_visible(db):
    cancel = queries.task_detail(db, fx.T_CANCEL)
    assert cancel["state"] == "cancelled" and cancel["turns"][0]["interrupted_by"] == "panel"
    assert cancel["turns"][0]["terminal_reason"] == "cancelled"


def test_timeline_filters_and_paging(db):
    assert [g["task_id"] for g in queries.timeline(db, chat=fx.CHAT_B)["groups"]] == [fx.T_OPEN, fx.T_CANCEL]
    assert [g["task_id"] for g in queries.timeline(db, state="open")["groups"]] == [fx.T_OPEN]
    assert [g["task_id"] for g in queries.timeline(db, state="cancelled")["groups"]] == [fx.T_CANCEL]
    memory = queries.timeline(db, kind="memory")["groups"]
    assert [g["task_id"] for g in memory] == [fx.T_LONG] and [c["tool"] for c in memory[0]["tool_calls"]] == ["memory"]
    first = queries.timeline(db, limit=2)
    assert len(first["groups"]) == 2 and first["next_before"] == first["groups"][-1]["created_at"]
    rest = queries.timeline(db, before=first["next_before"], limit=2)
    assert [g["task_id"] for g in rest["groups"]] == [fx.T_LONG, fx.T_OLD] and rest["next_before"] is None
    assert queries.task_detail(db, "tsk_missing") is None


def test_dublin_days_across_the_dst_change(db):
    out = queries.cost(db, "day", 30, DUBLIN, fx.NOW)
    by_day = {d["day"]: d for d in out["by_day"]}
    # 22:59 UTC on 24 Oct is 23:59 IST (24th); 23:30 UTC on 24 Oct is 00:30 IST (25th); 23:30 UTC on 25 Oct is
    # 23:30 GMT (25th).
    assert by_day["2026-10-24"]["total"]["cost_usd"] == 1.0
    assert by_day["2026-10-25"]["total"]["cost_usd"] == 6.0
    assert not by_day["2026-10-25"]["utc_day"]
    assert set(by_day["2026-10-26"]["by_model"]) == {"claude-sonnet-5-5", "claude-fable-5-1"}
    assert set(by_day["2026-10-26"]["by_kind"]) == {"quick", "task"}


def test_cost_totals_exclude_the_40_day_old_row(db):
    out = queries.cost(db, "chat", 30, DUBLIN, fx.NOW)
    today = sum(r["cost_usd"] for r in db.query("SELECT cost_usd FROM usage WHERE ts >= '2026-10-26T00:00:00Z'"))
    assert abs(out["totals"]["today"]["cost_usd"] - today) < 1e-9
    assert abs(out["totals"]["7d"]["cost_usd"] - (today + 7.0)) < 1e-9
    assert out["totals"]["30d"]["cost_usd"] == out["totals"]["7d"]["cost_usd"]
    assert 0 < out["totals"]["today"]["cache_hit_pct"] < 100
    chat_a = next(r for r in out["rows"] if r["chat_id"] == fx.CHAT_A)
    assert abs(chat_a["cost_usd"] - 0.4) < 1e-9 and chat_a["title"] == "Sunset lights"
    assert abs(chat_a["cost_all_usd"] - 0.4) < 1e-9


def test_days_older_than_retention_come_from_usage_daily(db, db_path):
    import sqlite3
    writer = sqlite3.connect(db_path)
    writer.execute("DELETE FROM usage WHERE ts < ?", (fx.ts(fx.NOW - timedelta(days=30)),))
    writer.commit()
    writer.close()
    out = queries.cost(db, "day", 60, DUBLIN, fx.NOW)
    old_day = (fx.NOW - timedelta(days=40)).date().isoformat()
    old = next(d for d in out["by_day"] if d["day"] == old_day)
    assert old["utc_day"] is True and old["total"]["cost_usd"] == 8.0 and "claude-opus-5-5" in old["by_model"]
    recent = queries.cost(db, "day", 60, DUBLIN, fx.NOW)["by_day"]
    assert sum(1 for d in recent if d["day"] == old_day) == 1


def test_cost_per_task(db):
    out = queries.cost(db, "task", 30, DUBLIN, fx.NOW)
    row = next(r for r in out["rows"] if r["task_id"] == fx.T_LONG)
    assert abs(row["cost_usd"] - 0.4) < 1e-9 and row["web_search_requests"] == 1 and row["state"] == "done"
    assert fx.T_OLD not in {r["task_id"] for r in out["rows"]}


def test_chats_unread_current_and_open_task(db):
    rows = {c["chat_id"]: c for c in queries.chats(db)}
    assert rows[fx.CHAT_A]["unread"] is True and rows[fx.CHAT_A]["current"] is True
    assert rows[fx.CHAT_B]["unread"] is False and rows[fx.CHAT_B]["task_state"] == "needs_approval"


def test_errors_newest_first_and_scrubbed(db):
    rows = queries.errors(db)
    assert [r["code"] for r in rows] == ["timeout", "overloaded"]
    assert "private_abc" not in rows[0]["message"] and "token=xyz" not in rows[0]["message"]
    assert "[redacted]" in rows[0]["message"]
    assert queries.error_count(db, fx.ts(fx.NOW - timedelta(hours=24))) == 1


def test_pending_lists_held_calls_without_tokens(db):
    out = queries.pending(db)
    assert [a["approval_id"] for a in out["approvals"]] == ["apr_3"]
    assert out["approvals"][0]["target"] == "climate.house" and "ha_token" not in out["approvals"][0]
    assert [i["interaction_id"] for i in out["interactions"]] == ["int_o1"]
    probes = queries.latest_probes(db)
    assert probes["ha_mcp"]["detail"]["pending_not_ours"] == 2
    assert queries.tools_cache(db)["tool_count"] == 77
    assert queries.home(db)["meta"]["entity_count"] == 4


def test_queries_module_selects_only_allowlisted_columns():
    source = Path(queries.__file__).read_text()
    assert not re.search(r"SELECT\s+\*|\.\*", source)
    words = set(re.findall(r"[a-z_]+", source))
    assert not words & set(FORBIDDEN_COLUMNS), words & set(FORBIDDEN_COLUMNS)
    for table in FORBIDDEN_TABLES:
        assert not re.search(rf"\b(FROM|JOIN)\s+{table}\b", source)


def test_text_fields_are_scrubbed_even_when_the_row_holds_a_secret(db_path, db):
    import sqlite3
    secret = "sk-proj-" + "a" * 32
    raw = sqlite3.connect(str(db_path))
    task_id = raw.execute("SELECT task_id FROM tasks ORDER BY created_at LIMIT 1").fetchone()[0]
    raw.execute("UPDATE tasks SET title = ?, summary = ? WHERE task_id = ?",
                (f"key {secret}", "AIza" + "B" * 35, task_id))
    raw.execute("UPDATE turns SET text = ?, final_text = ? WHERE task_id = ?",
                (f"my key is {secret}", "Bearer abc.def.ghi", task_id))
    raw.execute("UPDATE chats SET title = ?", (f"chat {secret}",))
    raw.commit()
    raw.close()
    blob = json.dumps([queries.task_detail(db, task_id), queries.chats(db), queries.timeline(db)])
    assert secret not in blob and "AIza" + "B" * 35 not in blob and "abc.def.ghi" not in blob


def test_home_selects_only_its_allowlisted_columns(db):
    assert queries.HOME_COLS == ("entity_id", "name", "state", "attributes_json", "last_changed", "updated_at")
    assert queries.HOME_META_COLS == ("period_s", "attempted_at", "fetched_at", "entity_count", "error_code")
    snapshot = queries.home(db)
    assert set(snapshot["rows"][0]) == {"entity_id", "name", "state", "attributes", "last_changed", "updated_at"}
    assert set(snapshot["meta"]) == set(queries.HOME_META_COLS)
