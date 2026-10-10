"""Restart reconciliation, run before the API opens (brief section 9, Restart row).

A turn in flight fails its task with service_restart (outcome unknown when a non-read-only call has no
end); tasks with no turn in flight keep their state; held HA calls owned by the previous process are
denied; SDK interactions expire; speech handed off without a playback ack is never repeated.
"""
from typing import TYPE_CHECKING, Any, Dict

from . import jsonlog, timeutil
from .store import dumps

if TYPE_CHECKING:
    from .turns import Engine

RESTART_NOTICE = {
    "en": "I restarted while working on {title}, so it may not have finished.",
    "zh": "我在处理「{title}」时重启了，可能没做完。",
}


async def recover(engine: "Engine") -> Dict[str, Any]:
    store = engine.store
    summary = {"tasks": 0, "turns": 0, "interactions": 0, "holds": 0, "deliveries": {}}
    now = timeutil.iso()
    running = store.all("SELECT * FROM turns WHERE state IN ('running', 'queued')")
    summary["turns"] = len(running)
    failed_tasks = {t["task_id"] for t in running if t["task_id"] and t["state"] == "running"}
    for turn in running:
        store.update("turns", "turn_id", turn["turn_id"], {
            "state": "failed", "terminal_reason": "service_restart", "ended_at": now})
    store.execute("UPDATE tool_calls SET status = 'unknown', ended_at = ? WHERE status = 'running' AND read_only = 0",
                  (now,))
    store.execute("UPDATE tool_calls SET status = 'error', ended_at = ? WHERE status = 'running' AND read_only = 1",
                  (now,))
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
    holds = await engine.ha.boot_reconcile()
    summary["holds"] = len(holds)
    for approval in holds:
        engine.interactions.on_terminal(approval)
    summary["deliveries"] = engine.outbox.on_restart()
    jsonlog.log("recovery summary", event="recovery", detail=dumps(summary))
    return summary
