"""Restart reconciliation, run before the API opens (brief section 9, Restart row).

A turn in flight fails its task with service_restart (outcome unknown when a non-read-only call has no
end); tasks with no turn in flight keep their state; held HA calls owned by the previous process are
denied; SDK interactions expire; open questions without 2-4 options are retired and their tasks done;
speech handed off without a playback ack is never repeated.
"""
from typing import TYPE_CHECKING, Any, Dict

from . import jsonlog, timeutil
from .interactions import has_choice
from .store import TERMINAL_STATES, dumps, loads

if TYPE_CHECKING:
    from .turns import Engine

RETIRED = "option-less question retired"

RESTART_NOTICE = {
    "en": "I restarted while working on {title}, so it may not have finished.",
    "zh": "我在处理「{title}」时重启了，可能没做完。",
}


async def recover(engine: "Engine") -> Dict[str, Any]:
    store = engine.store
    summary = {"tasks": 0, "turns": 0, "interactions": 0, "retired": 0, "holds": 0, "deliveries": {}}
    now = timeutil.iso()
    running = store.all("SELECT * FROM turns WHERE state IN ('running', 'queued')")
    summary["turns"] = len(running)
    failed_tasks = {t["task_id"] for t in running if t["task_id"] and t["state"] == "running"}
    for turn in running:
        store.update("turns", "turn_id", turn["turn_id"], {
            "state": "failed", "terminal_reason": "service_restart", "ended_at": now})
    store.execute("UPDATE tool_calls SET status = 'not_run', ended_at = ? WHERE status = 'running' AND kind = 'device'",
                  (now,))
    store.execute("UPDATE tool_calls SET status = 'unknown', ended_at = ? WHERE status IN ('running', 'dispatched') "
                  "AND read_only = 0", (now,))
    store.execute("UPDATE tool_calls SET status = 'error', ended_at = ? WHERE status IN ('running', 'dispatched') "
                  "AND read_only = 1", (now,))
    summary["interactions"] = store.execute(
        "UPDATE interactions SET state = 'expired', via = 'timeout', decided_at = ? "
        "WHERE state IN ('pending', 'uncorrelated') AND source = 'sdk'", (now,))
    store.execute("UPDATE deliveries SET state = 'superseded', updated_at = ? WHERE slot LIKE 'int:%' AND state IN "
                  "('pending', 'offered') AND slot IN (SELECT 'int:' || interaction_id FROM interactions "
                  "WHERE state = 'expired')", (now,))
    for task in store.all("SELECT * FROM tasks WHERE state = 'progress'"):
        waiting = store.scalar("SELECT COUNT(*) FROM interactions WHERE task_id = ? AND state IN ('pending', 'uncorrelated')",
                               (task["task_id"],))
        if not waiting:
            failed_tasks.add(task["task_id"])
    for task_id in failed_tasks:
        task = store.task(task_id)
        if task is None or task["state"] in ("done", "failed", "cancelled"):
            continue
        summary["tasks"] += 1
        unknown_calls = [r for r in store.all(
            "SELECT tool, target FROM tool_calls WHERE task_id = ? AND status = 'unknown' AND read_only = 0", (task_id,))]
        await engine.finish_task(task_id, "failed", error={"code": "service_restart",
                                                          "message": "the agent service restarted"},
                                 speak=RESTART_NOTICE[task["lang"]].format(title=task["title"] or "that"))
        calls = "; ".join(f"{c['tool']} {c['target'] or ''}".strip() for c in unknown_calls)
        note = f'[note: the service restarted while you were working on "{task["title"]}"; it did not finish'
        note += f"; these calls have an unknown result, do not repeat them: {calls}]" if calls else "]"
        engine.chats.add_note(task["chat_id"], "note", note)
        store.record_error("recovery", "service_restart", None, task_id=task_id, chat_id=task["chat_id"],
                           trace_id=task["trace_id"])
    summary["retired"] = await retire_optionless_questions(engine)
    holds = await engine.ha.boot_reconcile()
    summary["holds"] = len(holds)
    for approval in holds:
        engine.interactions.on_terminal(approval)
    summary["deliveries"] = engine.outbox.on_restart()
    jsonlog.log("recovery summary", event="recovery", detail=dumps(summary))
    return summary


async def retire_optionless_questions(engine: "Engine") -> int:
    """Open questions without 2-4 options are cancelled; a task left waiting on nothing ends done, read."""
    store = engine.store
    rows = [r for r in store.all("SELECT * FROM interactions WHERE kind = 'input' AND state IN ('pending', 'uncorrelated')")
            if not has_choice(loads(r["question_json"], {}))]
    tasks = []
    for row in rows:
        store.update("interactions", "interaction_id", row["interaction_id"], {
            "state": "cancelled", "decision_json": {"reason": RETIRED}, "decided_at": timeutil.iso()})
        engine.outbox.withdraw(row["device_id"], f"int:{row['interaction_id']}")
        if row["task_id"] not in tasks:
            tasks.append(row["task_id"])
    finished = 0
    for task_id in tasks:
        task = store.task(task_id)
        if task is None or task["state"] in TERMINAL_STATES or engine.interactions.open_for_task(task_id):
            continue
        await engine.finish_task(task_id, "done", reason=RETIRED, unread=False)
        finished += 1
    if rows:
        jsonlog.log("questions retired", event="recovery", outcome=RETIRED,
                    detail=f"interactions={len(rows)} tasks_done={finished}")
    return len(rows)
