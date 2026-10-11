"""A fixture database built with the merged agent-service schema (weixue_agent.store), so schema drift fails."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from weixue_agent.store import Store

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
SECRETS = ("hatok_SECRET_123", "ARGS_SECRET", "TRANSCRIPT_SECRET", "PROMPT_SECRET", "IDEM_SECRET", "DECISION_SECRET",
           "PAYLOAD_SECRET", "LEASE_SECRET", "SPEAK_SECRET", "QUESTION_SECRET", "private_abc", "token=xyz")
LONG_TEXT = "Make the living room lights a sunset automation " + "and keep going " * 20
DEVICE = "7c:0c:5f:42:4b:fc"
CHAT_A = "cht_01JAAAAAAAAAAAAAAAAAAAAAAA"
CHAT_B = "cht_01JBBBBBBBBBBBBBBBBBBBBBBB"
T_LONG = "tsk_01JLONGAAAAAAAAAAAAAAAAAAA"
T_CANCEL = "tsk_01JCANCELAAAAAAAAAAAAAAAA"
T_OPEN = "tsk_01JOPENAAAAAAAAAAAAAAAAAA"
T_OLD = "tsk_01JOLDAAAAAAAAAAAAAAAAAAA"


def ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def at(minutes: float) -> str:
    return ts(NOW - timedelta(hours=2) + timedelta(minutes=minutes))


def _usage(store: Store, when: str, task_id: str, chat_id: str, model: str, kind: str = "task", cost: float = 0.1,
           tokens=(1000, 200, 3000, 500), web: int = 0, turn_id: str = None) -> None:
    store.record_usage({"ts": when, "task_id": task_id, "chat_id": chat_id, "turn_id": turn_id, "turn_kind": kind,
                        "mode": "task" if kind == "task" else "quick", "effort": "high", "model": model,
                        "ttft_ms": 700, "input_tokens": tokens[0], "output_tokens": tokens[1],
                        "cache_read_tokens": tokens[2], "cache_creation_tokens": tokens[3],
                        "web_search_requests": web, "web_fetch_requests": 0, "cost_usd": cost})


def build(path: Path) -> Store:
    store = Store(path)
    ins = store.insert
    ins("devices", {"device_id": DEVICE, "current_chat_id": CHAT_A, "connected": 1, "last_seen_at": at(119),
                    "created_at": at(0), "updated_at": at(119)})
    ins("chats", {"chat_id": CHAT_A, "device_id": DEVICE, "mode": "task", "title": "Sunset lights",
                  "summary": "Living room sunset automation", "open_task_id": None, "activity_seq": 9,
                  "last_read_seq": 5, "last_activity_at": at(10), "created_at": at(0)})
    ins("chats", {"chat_id": CHAT_B, "device_id": DEVICE, "mode": "quick", "title": "Weather",
                  "open_task_id": T_OPEN, "activity_seq": 3, "last_read_seq": 3, "last_activity_at": at(60),
                  "created_at": at(20)})

    # The long task: two turns (a steer), seven tool calls of every kind, two held calls, two deliveries.
    ins("tasks", {"task_id": T_LONG, "chat_id": CHAT_A, "device_id": DEVICE, "state": "done", "title": LONG_TEXT[:32],
                  "summary": "Automation created", "lang": "en", "outcome": "done", "mode": "task",
                  "side_effects_json": json.dumps([{"tool": "ha_config_set_automation", "target": "automation.sunset",
                                                    "status": "ok"}]),
                  "trace_id": "a" * 32, "created_at": at(0), "updated_at": at(6), "finished_at": at(6),
                  "cost_usd": 0.4})
    ins("turns", {"turn_id": "trn_long1", "task_id": T_LONG, "chat_id": CHAT_A, "kind": "user", "state": "completed",
                  "routed": "new_task", "text": LONG_TEXT, "lang": "en", "trace_id": "a" * 32, "mode": "task",
                  "model": "claude-fable-5-1", "terminal_reason": "end_turn", "created_at": at(0), "started_at": at(0),
                  "ended_at": at(4), "ttft_ms": 640, "cost_usd": 0.3, "final_text": "Done. " * 40})
    ins("turns", {"turn_id": "trn_long2", "task_id": T_LONG, "chat_id": CHAT_A, "kind": "steer", "state": "completed",
                  "routed": "steer", "text": "make it 20 minutes before sunset", "lang": "en", "trace_id": "a" * 32,
                  "mode": "task", "model": "claude-fable-5-1", "interrupted_by": None, "created_at": at(2),
                  "ttft_ms": 500, "cost_usd": 0.1})
    calls = [
        ("tu_1", "ha", "ha_search", '{"query":"living room"}', None, 1, "ok", "3 entities", 0.2, 420, {}),
        ("tu_2", "ha", "ha_get_state", '{"entity_id":"light.living_room_ceiling_lights"}',
         "light.living_room_ceiling_lights", 1, "ok", "off", 0.4, 300, {}),
        ("tu_3", "server", "web_search", '{"query":"sunset dublin"}', None, 1, "ok", None, 0.6, None, {}),
        ("tu_4", "ha", "ha_config_set_automation", '{"alias":"Sunset"}', "automation.sunset", 0, "ok", "created",
         1.0, 41000, {"approval_id": "apr_1", "ha_token_id": "hat_1", "approval_decision": "approve",
                      "decided_via": "panel", "decided_by_device": DEVICE, "decided_at": at(1.6)}),
        ("tu_5", "ha", "ha_call_service", '{"domain":"lock","service":"unlock"}', "lock.front_door", 0, "error",
         "denied", 2.5, 9000, {"approval_id": "apr_2", "approval_decision": "deny", "decided_via": "voice",
                               "decided_at": at(2.6)}),
        ("tu_6", "memory", "memory", '{"command":"view"}', None, 1, "ok", "1 file", 3.0, 12, {}),
        ("tu_7", "app", "set_chat_title", '{"title":"Sunset lights"}', None, 1, "ok", "ok", 3.2, 2, {}),
    ]
    for tool_use_id, kind, tool, args, target, ro, status, result, minute, duration, extra in calls:
        ins("tool_calls", {"tool_use_id": tool_use_id, "task_id": T_LONG, "chat_id": CHAT_A,
                           "turn_id": "trn_long1" if minute < 2 else "trn_long2", "kind": kind, "tool": tool,
                           "args_summary": args, "target": target, "read_only": ro, "status": status,
                           "result_summary": result, "started_at": at(minute), "duration_ms": duration, **extra})
    for frm, to, reason, minute in [(None, "progress", "created", 0), ("progress", "needs_approval", "held", 1),
                                    ("needs_approval", "progress", "approved", 1.6), ("progress", "progress", "steer", 2),
                                    ("progress", "done", "end_turn", 6)]:
        ins("task_transitions", {"task_id": T_LONG, "from_state": frm, "to_state": to, "reason": reason,
                                 "ts": at(minute)})
    ins("interactions", {"interaction_id": "int_1", "task_id": T_LONG, "chat_id": CHAT_A, "device_id": DEVICE,
                         "kind": "approval", "source": "ha", "state": "decided", "question_json": '"QUESTION_SECRET"',
                         "action_json": "{}", "created_at": at(1), "via": "panel", "decided_at": at(1.6),
                         "tool_use_id": "tu_4"})
    ins("interactions", {"interaction_id": "int_2", "task_id": T_LONG, "chat_id": CHAT_A, "device_id": DEVICE,
                         "kind": "approval", "source": "ha", "state": "decided", "created_at": at(2.5), "via": "voice",
                         "decided_at": at(2.6), "tool_use_id": "tu_5"})
    for delivery_id, kind, state, playback, minute in [("dlv_1", "done", "delivered", "played", 6),
                                                       ("dlv_2", "progress", "delivered", "cut_off", 3)]:
        ins("deliveries", {"delivery_id": delivery_id, "device_id": DEVICE, "task_id": T_LONG, "chat_id": CHAT_A,
                           "kind": kind, "tier": "WHEN_IDLE", "slot": f"task:{T_LONG}", "payload_json": '"PAYLOAD_SECRET"',
                           "speak": "SPEAK_SECRET", "state": state, "lease_id": "LEASE_SECRET", "attempt": 1,
                           "playback": playback, "created_at": at(minute), "updated_at": at(minute)})
    ins("delivery_acks", {"delivery_id": "dlv_1", "device_id": DEVICE, "outcome": "played", "received_at": at(6.2)})
    ins("delivery_acks", {"delivery_id": "dlv_2", "device_id": DEVICE, "outcome": "cut_off", "cut_at_ms": 1800,
                          "reason": "barge-in", "received_at": at(3.1)})
    _usage(store, at(0.5), T_LONG, CHAT_A, "claude-sonnet-5-5", "quick", 0.05, (500, 80, 2000, 0), turn_id="trn_long1")
    _usage(store, at(1.5), T_LONG, CHAT_A, "claude-fable-5-1", "task", 0.2, (1200, 300, 6000, 1500), web=1,
           turn_id="trn_long1")
    _usage(store, at(3.5), T_LONG, CHAT_A, "claude-fable-5-1", "task", 0.15, (900, 250, 7000, 0), turn_id="trn_long2")

    # A cancelled quick task and an open task with a held call owned by this service.
    ins("tasks", {"task_id": T_CANCEL, "chat_id": CHAT_B, "device_id": DEVICE, "state": "cancelled", "title": "Weather",
                  "lang": "en", "outcome": "cancelled", "mode": "quick", "trace_id": "b" * 32, "created_at": at(20),
                  "updated_at": at(21), "finished_at": at(21), "cost_usd": 0.01})
    ins("turns", {"turn_id": "trn_c1", "task_id": T_CANCEL, "chat_id": CHAT_B, "kind": "user", "state": "cancelled",
                  "text": "what's the weather", "lang": "en", "trace_id": "b" * 32, "mode": "quick",
                  "model": "claude-sonnet-5-5", "terminal_reason": "cancelled", "interrupted_by": "panel",
                  "created_at": at(20), "cost_usd": 0.01})
    ins("task_transitions", {"task_id": T_CANCEL, "from_state": "progress", "to_state": "cancelled",
                             "reason": "interrupt", "ts": at(21)})
    _usage(store, at(20.2), T_CANCEL, CHAT_B, "claude-sonnet-5-5", "quick", 0.01, (300, 20, 0, 900))
    ins("tasks", {"task_id": T_OPEN, "chat_id": CHAT_B, "device_id": DEVICE, "state": "needs_approval",
                  "title": "Heating", "lang": "en", "mode": "task", "trace_id": "c" * 32, "created_at": at(60),
                  "updated_at": at(61)})
    ins("turns", {"turn_id": "trn_o1", "task_id": T_OPEN, "chat_id": CHAT_B, "kind": "user", "state": "running",
                  "text": "warm the house", "lang": "en", "trace_id": "c" * 32, "created_at": at(60)})
    ins("tool_calls", {"tool_use_id": "tu_o1", "task_id": T_OPEN, "chat_id": CHAT_B, "turn_id": "trn_o1", "kind": "ha",
                       "tool": "ha_call_service", "args_summary": '{"domain":"climate"}', "target": "climate.house",
                       "status": "pending", "started_at": at(60.5), "approval_id": "apr_3"})
    ins("approvals", {"approval_id": "apr_3", "device_id": DEVICE, "task_id": T_OPEN, "chat_id": CHAT_B,
                      "turn_id": "trn_o1", "tool_use_id": "tu_o1", "tool": "ha_call_service",
                      "args_json": '{"secret":"ARGS_SECRET"}', "args_hash": "h", "state": "pending",
                      "ha_token": "hatok_SECRET_123", "ha_expires_at": ts(NOW + timedelta(minutes=4)),
                      "created_at": ts(NOW - timedelta(minutes=1)), "updated_at": ts(NOW - timedelta(minutes=1))})
    ins("interactions", {"interaction_id": "int_o1", "task_id": T_OPEN, "chat_id": CHAT_B, "device_id": DEVICE,
                         "kind": "approval", "source": "ha", "state": "pending", "created_at": at(60.6)})

    # Usage across the Dublin DST change (2026-10-25 01:00 UTC) and a 40-day-old row.
    for when, cost in [(datetime(2026, 10, 24, 22, 59, tzinfo=timezone.utc), 1.0),
                       (datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc), 2.0),
                       (datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc), 4.0)]:
        _usage(store, ts(when), None, CHAT_B, "claude-sonnet-5-5", "quick", cost, (10, 10, 0, 0))
    ins("tasks", {"task_id": T_OLD, "chat_id": CHAT_A, "device_id": DEVICE, "state": "done", "title": "Old",
                  "lang": "en", "trace_id": "d" * 32, "created_at": ts(NOW - timedelta(days=40)),
                  "updated_at": ts(NOW - timedelta(days=40)), "cost_usd": 8.0})
    _usage(store, ts(NOW - timedelta(days=40)), T_OLD, CHAT_A, "claude-opus-5-5", "task", 8.0, (100, 100, 0, 0))

    # Rows the board must never show.
    ins("transcript", {"chat_id": CHAT_A, "epoch": 0, "seq": 1, "role": "user",
                       "content_json": '"TRANSCRIPT_SECRET"', "state": "sent", "created_at": at(0)})
    ins("chat_prompts", {"chat_id": CHAT_A, "epoch": 0, "system_text": "PROMPT_SECRET", "ha_tools_json": "[]",
                         "tools_json": "[]", "created_at": at(0)})
    ins("idempotency", {"key": "k", "scope": "s", "request_hash": "IDEM_SECRET", "ref_id": "r",
                        "response_json": '"IDEM_SECRET"', "created_at": at(0)})
    ins("decisions", {"decision_id": "d", "target_id": "t", "kind": "approve", "body_hash": "DECISION_SECRET",
                      "response_json": '"DECISION_SECRET"', "created_at": at(0)})

    store.insert("ha_tools_cache", {"id": 1, "tools_json": json.dumps([{"name": f"t{i}"} for i in range(77)]),
                                    "fetched_at": ts(NOW - timedelta(minutes=5))})
    store.insert("health_probes", {"ts": ts(NOW - timedelta(seconds=8)), "probe": "ha_mcp", "ok": 1, "latency_ms": 220,
                                   "detail_json": json.dumps({"tool_count": 77, "policy_version": 3, "wait_seconds": 8,
                                                              "pending_count": 3, "pending_not_ours": 2})})
    store.insert("health_probes", {"ts": ts(NOW - timedelta(seconds=8)), "probe": "mcp_endpoint", "ok": 1,
                                   "latency_ms": 3, "detail_json": "{}"})
    store.insert("errors", {"ts": ts(NOW - timedelta(hours=1)), "source": "ha", "code": "timeout",
                            "message": "call to http://ha.local:9584/private_abc failed token=xyz", "task_id": T_LONG})
    store.insert("errors", {"ts": ts(NOW - timedelta(hours=30)), "source": "model", "code": "overloaded",
                            "message": "529"})
    store.insert("connection_events", {"ts": at(100), "device_id": DEVICE, "kind": "open",
                                       "detail_json": json.dumps({"gateway_session_id": "s1"})})
    # Raw inserts: the board must scrub what the service stored.
    home = [("lock.front_door", "Front door", "locked", {}, 3 * 3600), ("light.lamp", "Lamp http://ha.local:9584/private_abc",
             "on", {"brightness": 128}, 90), ("climate.house", "House", "heat", {"current_temperature": 19.5,
             "temperature": 21, "hvac_action": "heating"}, 600), ("cover.curtain", "Curtain", "open",
             {"current_position": 36}, 86400)]
    for entity_id, name, state, attributes, age_s in home:
        store.execute("INSERT INTO home_snapshot (entity_id, name, state, attributes_json, last_changed, updated_at) "
                      "VALUES (?, ?, ?, ?, ?, ?)", (entity_id, name, state, json.dumps(attributes),
                                                    ts(NOW - timedelta(seconds=age_s)), ts(NOW - timedelta(seconds=age_s))))
    store.execute("INSERT INTO home_snapshot_meta (id, period_s, attempted_at, fetched_at, entity_count, error_code) "
                  "VALUES (1, 300, ?, ?, 4, NULL)", (ts(NOW - timedelta(seconds=60)), ts(NOW - timedelta(seconds=60))))
    return store
