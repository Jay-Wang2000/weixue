"""Every query the monitor runs. Columns come only from the allowlists below; text is scrubbed and clipped."""
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

from .db import ReadOnlyDb

TEXT_MAX = 120
MESSAGE_MAX = 200
TIMELINE_MAX = 200
RETENTION_DAYS = 30
OPEN_STATES = ("done", "failed", "cancelled")
HELD_APPROVAL_STATES = ("in_flight", "pending", "awaiting_decision_after_wait")

TASK_COLS = ("task_id", "chat_id", "device_id", "state", "title", "summary", "outcome", "mode", "result_tier",
             "retry_of", "side_effects_json", "created_at", "updated_at", "finished_at", "cost_usd")
TURN_COLS = ("turn_id", "task_id", "chat_id", "kind", "state", "routed", "mode", "model", "terminal_reason",
             "reply_end_reason", "interrupted_by", "ttft_ms", "cost_usd", "created_at", "started_at", "ended_at",
             "reply_end_at")
TURN_TEXT_COLS = ("text", "final_text")
TOOL_CALL_COLS = ("tool_use_id", "task_id", "chat_id", "turn_id", "kind", "tool", "args_summary", "target",
                  "read_only", "status", "result_summary", "started_at", "ended_at", "duration_ms",
                  "approval_decision", "decided_via", "decided_by_device", "decided_at")
TRANSITION_COLS = ("id", "task_id", "turn_id", "from_state", "to_state", "reason", "ts")
INTERACTION_COLS = ("interaction_id", "task_id", "chat_id", "turn_id", "kind", "source", "state", "via",
                    "created_at", "expires_at", "decided_at")
DELIVERY_COLS = ("delivery_id", "task_id", "turn_id", "kind", "tier", "slot", "state", "attempt", "playback",
                 "created_at", "updated_at")
ACK_COLS = ("delivery_id", "outcome", "reason", "cut_at_ms", "received_at")
USAGE_COLS = ("ts", "task_id", "chat_id", "turn_id", "turn_kind", "mode", "effort", "model", "ttft_ms",
              "input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens",
              "web_search_requests", "web_fetch_requests", "cost_usd")
USAGE_DAILY_COLS = ("day", "model", "turn_kind", "results", "input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_creation_tokens", "web_search_requests", "cost_usd")
CHAT_COLS = ("chat_id", "device_id", "title", "summary", "mode", "open_task_id", "last_activity_at",
             "activity_seq", "last_read_seq", "created_at")
DEVICE_COLS = ("device_id", "current_chat_id", "connected", "busy", "last_seen_at")
ERROR_COLS = ("id", "ts", "source", "code", "message", "task_id", "chat_id", "trace_id")
APPROVAL_COLS = ("approval_id", "task_id", "chat_id", "tool", "state", "decision", "via", "ha_expires_at",
                 "created_at")
PROBE_COLS = ("id", "ts", "probe", "ok", "latency_ms", "detail_json")
CONNECTION_COLS = ("id", "ts", "device_id", "kind", "detail_json")
HOME_COLS = ("entity_id", "name", "state", "last_changed", "fetched_at")

TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_creation_tokens")
_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.I)
_SECRETISH = re.compile(r"(private_\S+|token=\S+|sk-ant-\S+|Bearer\s+\S+|\bsk-[A-Za-z0-9_\-]{20,}|"
                        r"\bAIza[0-9A-Za-z_\-]{30,}|\bgh[pousr]_[A-Za-z0-9]{30,}|\bxox[abprs]-[A-Za-z0-9\-]{10,}|"
                        r"\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+|"
                        r"-----BEGIN [A-Z ]*PRIVATE KEY-----)", re.I)


def cols(names: Sequence[str], alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return ", ".join(prefix + n for n in names)


def clip(value: Optional[str], limit: int = TEXT_MAX) -> Optional[str]:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


def text(value: Optional[str], limit: int = TEXT_MAX) -> Optional[str]:
    """User or model text: token-like strings replaced, then clipped."""
    return scrub(value, limit)


def scrub(value: Optional[str], limit: int = MESSAGE_MAX) -> Optional[str]:
    """Clipped text with URLs and token-like strings replaced."""
    if value is None:
        return None
    return clip(_SECRETISH.sub("[redacted]", _URL.sub("[url]", str(value))), limit)


def _json(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except ValueError:
        return default


def _scrub_obj(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrub_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_obj(v) for v in value]
    return value


def _marks(values: Iterable[Any]) -> str:
    return ", ".join("?" for _ in values)


def _by(rows: List[Dict[str, Any]], key: str) -> Dict[Any, List[Dict[str, Any]]]:
    out: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        out[row[key]].append(row)
    return out


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_day(ts: str, tz: ZoneInfo) -> str:
    return _parse(ts).astimezone(tz).date().isoformat()


def _empty_tokens() -> Dict[str, Any]:
    return {**{f: 0 for f in TOKEN_FIELDS}, "web_search_requests": 0, "web_fetch_requests": 0, "cost_usd": 0.0,
            "requests": 0}


def _add(total: Dict[str, Any], row: Dict[str, Any]) -> None:
    for f in (*TOKEN_FIELDS, "web_search_requests", "web_fetch_requests"):
        total[f] += row.get(f) or 0
    total["cost_usd"] += row.get("cost_usd") or 0.0
    total["requests"] += row.get("results", 1) or 0


def cache_hit(total: Dict[str, Any]) -> Optional[float]:
    seen = total["input_tokens"] + total["cache_read_tokens"] + total["cache_creation_tokens"]
    return round(100.0 * total["cache_read_tokens"] / seen, 1) if seen else None


def _finish(total: Dict[str, Any]) -> Dict[str, Any]:
    total["cost_usd"] = round(total["cost_usd"], 6)
    total["cache_hit_pct"] = cache_hit(total)
    return total


# ---------------------------------------------------------------------- timeline


def _turn(row: Dict[str, Any]) -> Dict[str, Any]:
    row["text"] = text(row.get("text"))
    row["final_text"] = text(row.get("final_text"))
    return row


def _tool_call(row: Dict[str, Any]) -> Dict[str, Any]:
    row["args_summary"] = _scrub_obj(_json(row.get("args_summary"), row.get("args_summary")))
    row["result_summary"] = scrub(row.get("result_summary"))
    row["read_only"] = bool(row.get("read_only"))
    return row


def _task(row: Dict[str, Any]) -> Dict[str, Any]:
    row["title"] = text(row.get("title"))
    row["summary"] = text(row.get("summary"))
    row["side_effects"] = _scrub_obj(_json(row.pop("side_effects_json", None), []))
    return row


def _children(db: ReadOnlyDb, task_ids: List[str], kind: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    if not task_ids:
        return {}
    m = _marks(task_ids)
    text_cols = ", ".join(f"substr({c}, 1, {TEXT_MAX + 1}) AS {c}" for c in TURN_TEXT_COLS)
    turns = _by([_turn(r) for r in db.query(
        f"SELECT {cols(TURN_COLS)}, {text_cols} FROM turns WHERE task_id IN ({m}) ORDER BY created_at",
        task_ids)], "task_id")
    tool_sql = f"SELECT {cols(TOOL_CALL_COLS)} FROM tool_calls WHERE task_id IN ({m})"
    params: List[Any] = list(task_ids)
    if kind:
        tool_sql += " AND kind = ?"
        params.append(kind)
    tools = _by([_tool_call(r) for r in db.query(tool_sql + " ORDER BY started_at", params)], "task_id")
    transitions = _by(db.query(
        f"SELECT {cols(TRANSITION_COLS)} FROM task_transitions WHERE task_id IN ({m}) ORDER BY id", task_ids),
        "task_id")
    interactions = _by(db.query(
        f"SELECT {cols(INTERACTION_COLS)} FROM interactions WHERE task_id IN ({m}) ORDER BY created_at", task_ids),
        "task_id")
    deliveries = db.query(
        f"SELECT {cols(DELIVERY_COLS)} FROM deliveries WHERE task_id IN ({m}) ORDER BY gseq", task_ids)
    acks: Dict[str, List[Dict[str, Any]]] = {}
    if deliveries:
        ids = [d["delivery_id"] for d in deliveries]
        acks = _by([dict(a, reason=scrub(a.get("reason"))) for a in db.query(
            f"SELECT {cols(ACK_COLS)} FROM delivery_acks WHERE delivery_id IN ({_marks(ids)}) ORDER BY id", ids)],
            "delivery_id")
    for d in deliveries:
        d["acks"] = acks.get(d["delivery_id"], [])
    deliveries_by = _by(deliveries, "task_id")
    usage = {r["task_id"]: r for r in db.query(
        f"SELECT task_id, COUNT(*) AS requests, {', '.join(f'SUM({f}) AS {f}' for f in TOKEN_FIELDS)}, "
        "SUM(web_search_requests) AS web_search_requests, SUM(web_fetch_requests) AS web_fetch_requests, "
        f"SUM(cost_usd) AS cost_usd FROM usage WHERE task_id IN ({m}) GROUP BY task_id", task_ids)}
    out = {}
    for task_id in task_ids:
        total = _empty_tokens()
        if task_id in usage:
            row = usage[task_id]
            for f in total:
                total[f] = row.get(f) or 0
        out[task_id] = {
            "turns": turns.get(task_id, []), "tool_calls": tools.get(task_id, []),
            "transitions": transitions.get(task_id, []), "interactions": interactions.get(task_id, []),
            "deliveries": deliveries_by.get(task_id, []), "usage": _finish(total),
        }
    return out


def _group(task: Dict[str, Any], children: Dict[str, Any]) -> Dict[str, Any]:
    group = {**_task(task), **children}
    turns = children["turns"]
    group["model"] = next((t["model"] for t in reversed(turns) if t.get("model")), None)
    end = _parse(task["finished_at"]) if task.get("finished_at") else None
    start = _parse(task["created_at"])
    group["duration_ms"] = int((end - start).total_seconds() * 1000) if end and start else None
    group["counts"] = {"turns": len(turns), "tool_calls": len(children["tool_calls"]),
                       "steers": sum(1 for t in turns if t["kind"] == "steer"),
                       "deliveries": len(children["deliveries"])}
    return group


def timeline(db: ReadOnlyDb, chat: Optional[str] = None, state: Optional[str] = None, kind: Optional[str] = None,
             before: Optional[str] = None, limit: int = 100) -> Dict[str, Any]:
    """Tasks newest first with their turns, tool calls, transitions, interactions, deliveries and token totals."""
    limit = max(1, min(int(limit), TIMELINE_MAX))
    where, params = [], []
    if chat:
        where.append("chat_id = ?")
        params.append(chat)
    if state:
        if state == "open":
            where.append(f"state NOT IN ({_marks(OPEN_STATES)})")
            params.extend(OPEN_STATES)
        else:
            where.append("state = ?")
            params.append(state)
    if kind:
        where.append("task_id IN (SELECT task_id FROM tool_calls WHERE kind = ?)")
        params.append(kind)
    if before:
        where.append("created_at < ?")
        params.append(before)
    sql = f"SELECT {cols(TASK_COLS)} FROM tasks"
    if where:
        sql += " WHERE " + " AND ".join(where)
    tasks = db.query(sql + " ORDER BY created_at DESC LIMIT ?", [*params, limit + 1])
    more = len(tasks) > limit
    tasks = tasks[:limit]
    children = _children(db, [t["task_id"] for t in tasks], kind)
    groups = [_group(t, children[t["task_id"]]) for t in tasks]
    return {"groups": groups, "next_before": tasks[-1]["created_at"] if more and tasks else None}


def task_detail(db: ReadOnlyDb, task_id: str) -> Optional[Dict[str, Any]]:
    task = db.one(f"SELECT {cols(TASK_COLS)} FROM tasks WHERE task_id = ?", (task_id,))
    if task is None:
        return None
    group = _group(task, _children(db, [task_id])[task_id])
    group["usage_rows"] = db.query(f"SELECT {cols(USAGE_COLS)} FROM usage WHERE task_id = ? ORDER BY id", (task_id,))
    return group


# ---------------------------------------------------------------------- cost


def cost(db: ReadOnlyDb, group: str, days: int, tz: ZoneInfo, now: datetime) -> Dict[str, Any]:
    """Totals for today / 7 d / 30 d and rows per chat, task or day (Dublin days, split by model and turn kind).
    Days older than the oldest `usage` row come from `usage_daily` (UTC days, marked utc_day)."""
    days = max(1, min(int(days), 3660))
    today = now.astimezone(tz).date()
    first_day = today - timedelta(days=days - 1)
    since = iso(datetime.combine(first_day, datetime.min.time(), tz))
    usage = db.query(f"SELECT {cols(USAGE_COLS)} FROM usage WHERE ts >= ? ORDER BY ts", (since,))
    oldest = db.scalar("SELECT MIN(ts) AS ts FROM usage")
    oldest_day = local_day(oldest, tz) if oldest else today.isoformat()

    totals = {k: _empty_tokens() for k in ("today", "7d", "30d")}
    for row in usage:
        age = (today - datetime.fromisoformat(local_day(row["ts"], tz)).date()).days
        if age == 0:
            _add(totals["today"], row)
        if age < 7:
            _add(totals["7d"], row)
        if age < 30:
            _add(totals["30d"], row)
    out: Dict[str, Any] = {"group": group, "days": days, "tz": str(tz),
                           "totals": {k: _finish(v) for k, v in totals.items()}}

    by_day: Dict[str, Dict[str, Any]] = {}
    for row in usage:
        day = local_day(row["ts"], tz)
        entry = by_day.setdefault(day, {"day": day, "utc_day": False, "total": _empty_tokens(), "by_model": {},
                                        "by_kind": {}})
        _add(entry["total"], row)
        _add(entry["by_model"].setdefault(row["model"], _empty_tokens()), row)
        _add(entry["by_kind"].setdefault(row["turn_kind"], _empty_tokens()), row)
    for row in db.query(f"SELECT {cols(USAGE_DAILY_COLS)} FROM usage_daily WHERE day >= ? AND day < ? ORDER BY day",
                        (first_day.isoformat(), oldest_day)):
        entry = by_day.setdefault(row["day"], {"day": row["day"], "utc_day": True, "total": _empty_tokens(),
                                               "by_model": {}, "by_kind": {}})
        row = dict(row, web_fetch_requests=0)
        _add(entry["total"], row)
        _add(entry["by_model"].setdefault(row["model"], _empty_tokens()), row)
        _add(entry["by_kind"].setdefault(row["turn_kind"], _empty_tokens()), row)
    for entry in by_day.values():
        _finish(entry["total"])
        for part in (*entry["by_model"].values(), *entry["by_kind"].values()):
            _finish(part)
    out["by_day"] = [by_day[d] for d in sorted(by_day, reverse=True)]

    if group == "chat":
        sums: Dict[str, Dict[str, Any]] = defaultdict(_empty_tokens)
        for row in usage:
            _add(sums[row["chat_id"]], row)
        chats = {c["chat_id"]: c for c in db.query(
            "SELECT chat_id, title, SUM(cost) AS cost_all_usd FROM (SELECT c.chat_id, c.title, t.cost_usd AS cost "
            "FROM chats c LEFT JOIN turns t ON t.chat_id = c.chat_id) GROUP BY chat_id")}
        rows = []
        for chat_id in set(sums) | set(chats):
            chat = chats.get(chat_id, {})
            rows.append({"chat_id": chat_id, "title": text(chat.get("title")),
                         "cost_all_usd": round(chat.get("cost_all_usd") or 0.0, 6),
                         **_finish(sums.get(chat_id, _empty_tokens()))})
        out["rows"] = sorted(rows, key=lambda r: (r["cost_usd"], r["cost_all_usd"]), reverse=True)
    elif group == "task":
        sums = defaultdict(_empty_tokens)
        for row in usage:
            _add(sums[row["task_id"]], row)
        tasks = db.query(f"SELECT task_id, chat_id, title, state, mode, created_at, cost_usd AS cost_all_usd "
                         "FROM tasks WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?", (since, TIMELINE_MAX))
        out["rows"] = [{**t, "title": text(t["title"]), **_finish(sums.get(t["task_id"], _empty_tokens()))}
                       for t in tasks]
    else:
        out["rows"] = out["by_day"]
    return out


def cost_today(db: ReadOnlyDb, tz: ZoneInfo, now: datetime) -> float:
    start = datetime.combine(now.astimezone(tz).date(), datetime.min.time(), tz)
    return round(db.scalar("SELECT COALESCE(SUM(cost_usd), 0) AS c FROM usage WHERE ts >= ?", (iso(start),)) or 0.0, 6)


# ---------------------------------------------------------------------- chats, errors, connections, mcp, home


def chats(db: ReadOnlyDb, limit: int = 100) -> List[Dict[str, Any]]:
    rows = db.query(f"SELECT {cols(CHAT_COLS, 'c')}, t.state AS task_state, t.outcome AS task_outcome, "
                    "t.side_effects_json AS side_effects_json FROM chats c "
                    "LEFT JOIN tasks t ON t.task_id = c.open_task_id ORDER BY c.last_activity_at DESC LIMIT ?",
                    (limit,))
    current = {d["current_chat_id"] for d in db.query(f"SELECT {cols(DEVICE_COLS)} FROM devices")}
    for row in rows:
        row["title"] = text(row["title"])
        row["summary"] = text(row["summary"])
        row["unread"] = (row["activity_seq"] or 0) > (row["last_read_seq"] or 0)
        row["current"] = row["chat_id"] in current
        row["side_effects"] = _scrub_obj(_json(row.pop("side_effects_json"), []))
    return rows


def devices(db: ReadOnlyDb) -> List[Dict[str, Any]]:
    return db.query(f"SELECT {cols(DEVICE_COLS)} FROM devices ORDER BY last_seen_at DESC")


def errors(db: ReadOnlyDb, limit: int = 100) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 500))
    rows = db.query(f"SELECT {cols(ERROR_COLS)} FROM errors ORDER BY ts DESC, id DESC LIMIT ?", (limit,))
    for row in rows:
        row["message"] = scrub(row["message"])
    return rows


def error_count(db: ReadOnlyDb, since: str) -> int:
    return db.scalar("SELECT COUNT(*) AS n FROM errors WHERE ts >= ?", (since,)) or 0


def open_tasks(db: ReadOnlyDb) -> int:
    return db.scalar(f"SELECT COUNT(*) AS n FROM tasks WHERE state NOT IN ({_marks(OPEN_STATES)})", OPEN_STATES) or 0


def _probe(row: Dict[str, Any]) -> Dict[str, Any]:
    detail = _scrub_obj(_json(row.pop("detail_json", None), {}))
    return {**row, "ok": bool(row["ok"]), "detail": detail if isinstance(detail, dict) else {}}


def latest_probes(db: ReadOnlyDb) -> Dict[str, Dict[str, Any]]:
    rows = db.query(f"SELECT {cols(PROBE_COLS)} FROM health_probes WHERE id IN "
                    "(SELECT MAX(id) FROM health_probes GROUP BY probe)")
    return {r["probe"]: _probe(r) for r in rows}


def connection_events(db: ReadOnlyDb, limit: int = 20) -> List[Dict[str, Any]]:
    rows = db.query(f"SELECT {cols(CONNECTION_COLS)} FROM connection_events ORDER BY id DESC LIMIT ?", (limit,))
    for row in rows:
        row["detail"] = _scrub_obj(_json(row.pop("detail_json"), {}))
    return rows


def pending(db: ReadOnlyDb) -> Dict[str, Any]:
    approvals = db.query(f"SELECT {cols(APPROVAL_COLS)} FROM approvals WHERE state IN ({_marks(HELD_APPROVAL_STATES)}) "
                         "ORDER BY created_at", HELD_APPROVAL_STATES)
    interactions = db.query(f"SELECT {cols(INTERACTION_COLS)} FROM interactions WHERE state = 'pending' "
                            "ORDER BY created_at")
    ids = [a["approval_id"] for a in approvals]
    targets = {r["approval_id"]: r["target"] for r in db.query(
        f"SELECT approval_id, target FROM tool_calls WHERE approval_id IN ({_marks(ids)})", ids)} if ids else {}
    for row in approvals:
        row["target"] = targets.get(row["approval_id"])
    return {"approvals": approvals, "interactions": interactions}


def tools_cache(db: ReadOnlyDb) -> Optional[Dict[str, Any]]:
    return db.one("SELECT json_array_length(tools_json) AS tool_count, fetched_at FROM ha_tools_cache WHERE id = 1")


def home(db: ReadOnlyDb) -> Optional[List[Dict[str, Any]]]:
    """Rows of home_snapshot, None while the table does not exist."""
    if not db.has_table("home_snapshot"):
        return None
    return db.query(f"SELECT {cols(HOME_COLS)} FROM home_snapshot ORDER BY entity_id")
