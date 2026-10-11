"""SQLite store: chat index, transcripts, tasks, turns, events, interactions, approvals, outbox, monitor data.

One connection, WAL journal, autocommit with explicit transactions under a lock. The monitor board
opens the same file read-only.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import jsonlog, timeutil

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE devices (
  device_id TEXT PRIMARY KEY,
  current_chat_id TEXT,
  previous_chat_id TEXT,
  voice_interaction_id TEXT,
  voice_window_until TEXT,
  connected INTEGER NOT NULL DEFAULT 0,
  busy INTEGER NOT NULL DEFAULT 0,
  last_seen_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE chats (
  chat_id TEXT PRIMARY KEY,
  device_id TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'quick',
  effort TEXT NOT NULL DEFAULT 'low',
  task_model TEXT,
  title TEXT,
  title_source TEXT NOT NULL DEFAULT 'placeholder',
  summary TEXT,
  open_task_id TEXT,
  last_seq INTEGER NOT NULL DEFAULT 0,
  activity_seq INTEGER NOT NULL DEFAULT 0,
  last_read_seq INTEGER NOT NULL DEFAULT 0,
  last_activity_at TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX chats_device ON chats(device_id, last_activity_at);
CREATE TABLE tasks (
  task_id TEXT PRIMARY KEY,
  chat_id TEXT NOT NULL,
  device_id TEXT NOT NULL,
  state TEXT NOT NULL,
  title TEXT,
  summary TEXT,
  lang TEXT NOT NULL,
  progress_json TEXT,
  outcome TEXT,
  error_json TEXT,
  side_effects_json TEXT NOT NULL DEFAULT '[]',
  result_tier TEXT NOT NULL DEFAULT 'WHEN_IDLE',
  mode TEXT NOT NULL DEFAULT 'quick',
  retry_of TEXT,
  retry_allowed INTEGER NOT NULL DEFAULT 1,
  trace_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  finished_at TEXT,
  cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX tasks_device ON tasks(device_id, created_at);
CREATE INDEX tasks_chat ON tasks(chat_id, created_at);
CREATE INDEX tasks_state ON tasks(state);
CREATE TABLE turns (
  turn_id TEXT PRIMARY KEY,
  task_id TEXT,
  chat_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  state TEXT NOT NULL,
  routed TEXT,
  text TEXT NOT NULL,
  lang TEXT NOT NULL,
  trace_id TEXT NOT NULL,
  idempotency_key TEXT,
  epoch INTEGER,
  mode TEXT,
  model TEXT,
  terminal_reason TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  ended_at TEXT,
  reply_end_at TEXT,
  reply_end_reason TEXT,
  interrupted_by TEXT,
  final_text TEXT,
  late_text TEXT,
  ttft_ms INTEGER,
  cost_usd REAL NOT NULL DEFAULT 0
);
CREATE INDEX turns_chat ON turns(chat_id, created_at);
CREATE INDEX turns_task ON turns(task_id);
CREATE INDEX turns_state ON turns(state);
CREATE TABLE chat_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT NOT NULL,
  consumed_by TEXT
);
CREATE INDEX chat_notes_chat ON chat_notes(chat_id, consumed_by);
CREATE TABLE tool_calls (
  tool_use_id TEXT PRIMARY KEY,
  task_id TEXT,
  chat_id TEXT,
  turn_id TEXT,
  epoch INTEGER,
  kind TEXT NOT NULL DEFAULT 'ha',
  tool TEXT NOT NULL,
  args_summary TEXT,
  target TEXT,
  read_only INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  result_summary TEXT,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  duration_ms INTEGER,
  approval_id TEXT,
  ha_token_id TEXT,
  approval_decision TEXT,
  decided_via TEXT,
  decided_by_device TEXT,
  decided_at TEXT,
  dispatch_id TEXT
);
CREATE INDEX tool_calls_task ON tool_calls(task_id);
CREATE INDEX tool_calls_turn ON tool_calls(turn_id);
CREATE INDEX tool_calls_started ON tool_calls(started_at);
CREATE TABLE chat_events (
  chat_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts TEXT NOT NULL,
  type TEXT NOT NULL,
  task_id TEXT,
  turn_id TEXT,
  trace_id TEXT NOT NULL,
  data_json TEXT NOT NULL,
  PRIMARY KEY (chat_id, seq)
);
CREATE INDEX chat_events_ts ON chat_events(ts);
CREATE TABLE task_transitions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  turn_id TEXT,
  from_state TEXT,
  to_state TEXT NOT NULL,
  reason TEXT,
  ts TEXT NOT NULL
);
CREATE INDEX task_transitions_task ON task_transitions(task_id);
CREATE TABLE interactions (
  interaction_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  chat_id TEXT NOT NULL,
  turn_id TEXT,
  device_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  source TEXT NOT NULL,
  state TEXT NOT NULL,
  question_json TEXT,
  action_json TEXT,
  voice_can_approve INTEGER NOT NULL DEFAULT 0,
  tool_use_id TEXT,
  external_ref TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  spoken_at TEXT,
  decision_json TEXT,
  decision_id TEXT,
  via TEXT,
  decided_at TEXT
);
CREATE INDEX interactions_task ON interactions(task_id);
CREATE INDEX interactions_state ON interactions(state);
CREATE TABLE approvals (
  approval_id TEXT PRIMARY KEY,
  device_id TEXT,
  task_id TEXT,
  chat_id TEXT,
  turn_id TEXT,
  tool_use_id TEXT,
  tool TEXT NOT NULL,
  args_json TEXT NOT NULL,
  args_hash TEXT NOT NULL,
  state TEXT NOT NULL,
  ha_token TEXT,
  token_source TEXT,
  ha_expires_at TEXT,
  decision TEXT,
  via TEXT,
  decided_at TEXT,
  result_text TEXT,
  waiters_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX approvals_state ON approvals(state);
CREATE TABLE deliveries (
  gseq INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_id TEXT NOT NULL UNIQUE,
  device_id TEXT NOT NULL,
  task_id TEXT,
  chat_id TEXT,
  turn_id TEXT,
  kind TEXT NOT NULL,
  tier TEXT NOT NULL,
  slot TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  speak TEXT,
  lang TEXT,
  state TEXT NOT NULL,
  lease_id TEXT,
  lease_until TEXT,
  not_before TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  proactive_id TEXT,
  playback TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX deliveries_device ON deliveries(device_id, state);
CREATE TABLE delivery_acks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_id TEXT NOT NULL,
  device_id TEXT,
  outcome TEXT NOT NULL,
  boot INTEGER,
  device_seq INTEGER,
  sentence_id TEXT,
  cut_at_ms INTEGER,
  reason TEXT,
  proactive_id TEXT,
  detail TEXT,
  received_at TEXT NOT NULL
);
CREATE INDEX delivery_acks_delivery ON delivery_acks(delivery_id);
CREATE INDEX delivery_acks_ts ON delivery_acks(received_at);
CREATE TABLE idempotency (
  key TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  response_json TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE decisions (
  decision_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE ha_tools_cache (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  tools_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL
);
CREATE TABLE usage (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  task_id TEXT,
  chat_id TEXT,
  turn_id TEXT,
  epoch INTEGER,
  turn_kind TEXT NOT NULL,
  mode TEXT,
  effort TEXT,
  model TEXT NOT NULL,
  request_id TEXT,
  ttft_ms INTEGER,
  stop_reason TEXT,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  web_search_requests INTEGER NOT NULL DEFAULT 0,
  web_fetch_requests INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  duration_ms INTEGER,
  input_transformations_json TEXT
);
CREATE INDEX usage_ts ON usage(ts);
CREATE INDEX usage_task ON usage(task_id);
CREATE TABLE usage_daily (
  day TEXT NOT NULL,
  model TEXT NOT NULL,
  turn_kind TEXT NOT NULL,
  results INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens INTEGER NOT NULL DEFAULT 0,
  cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
  web_search_requests INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (day, model, turn_kind)
);
CREATE TABLE connection_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  device_id TEXT,
  kind TEXT NOT NULL,
  detail_json TEXT
);
CREATE INDEX connection_events_ts ON connection_events(ts);
CREATE TABLE health_probes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  probe TEXT NOT NULL,
  ok INTEGER NOT NULL,
  latency_ms INTEGER,
  detail_json TEXT
);
CREATE INDEX health_probes_ts ON health_probes(probe, ts);
CREATE TABLE errors (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  code TEXT NOT NULL,
  message TEXT,
  task_id TEXT,
  chat_id TEXT,
  turn_id TEXT,
  trace_id TEXT
);
CREATE INDEX errors_ts ON errors(ts);
CREATE TABLE home_snapshot (
  entity_id TEXT PRIMARY KEY,
  name TEXT,
  state TEXT NOT NULL,
  attributes_json TEXT,
  last_changed TEXT,
  updated_at TEXT NOT NULL
);
CREATE TABLE home_snapshot_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  period_s REAL NOT NULL,
  attempted_at TEXT NOT NULL,
  fetched_at TEXT,
  entity_count INTEGER NOT NULL DEFAULT 0,
  error_code TEXT
);
CREATE TABLE transcript (
  chat_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  seq INTEGER NOT NULL,
  role TEXT NOT NULL,
  content_json TEXT NOT NULL,
  model TEXT,
  turn_id TEXT,
  request_id TEXT,
  state TEXT NOT NULL,
  rewritten INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (chat_id, epoch, seq)
);
CREATE TABLE chat_prompts (
  chat_id TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  system_text TEXT NOT NULL,
  ha_tools_json TEXT NOT NULL,
  tools_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (chat_id, epoch)
);
"""

_MIGRATIONS = {
    2: ("ALTER TABLE approvals ADD COLUMN token_source TEXT;"
        "ALTER TABLE tool_calls ADD COLUMN dispatch_id TEXT;"),
    3: ("CREATE TABLE home_snapshot (entity_id TEXT PRIMARY KEY, name TEXT, state TEXT NOT NULL, "
        "attributes_json TEXT, last_changed TEXT, updated_at TEXT NOT NULL);"
        "CREATE TABLE home_snapshot_meta (id INTEGER PRIMARY KEY CHECK (id = 1), period_s REAL NOT NULL, "
        "attempted_at TEXT NOT NULL, fetched_at TEXT, entity_count INTEGER NOT NULL DEFAULT 0, error_code TEXT);"),
}
# Free-text columns: redacted before they are first written (the transcript is composed from redacted inputs).
# `*_json` columns and JSON_COLUMNS take structured values: each string inside is redacted before serialization.
SCRUBBED = {
    "chats": {"title", "summary"},
    "tasks": {"title", "summary", "progress_json", "error_json", "side_effects_json"},
    "turns": {"text", "final_text", "late_text"},
    "chat_notes": {"text"},
    "tool_calls": {"args_summary", "target", "result_summary"},
    "interactions": {"question_json", "action_json", "decision_json"},
    "approvals": {"result_text"},
    "deliveries": {"payload_json", "speak"},
    "errors": {"message"},
    "connection_events": {"detail_json"},
    "health_probes": {"detail_json"},
    "home_snapshot": {"name", "attributes_json"},
}
JSON_COLUMNS = {"args_summary"}

TERMINAL_STATES = ("done", "failed", "cancelled")
RETENTION_TABLES = {
    "chat_events": "ts",
    "task_transitions": "ts",
    "tool_calls": "started_at",
    "usage": "ts",
    "connection_events": "ts",
    "health_probes": "ts",
    "errors": "ts",
    "delivery_acks": "received_at",
}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def loads_or(value: Optional[str], default: Any) -> Any:
    """`default` also for a malformed value."""
    try:
        return loads(value, default)
    except ValueError:
        return default


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def close(self) -> None:
        with self._lock:
            self.db.close()

    def _migrate(self) -> None:
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"database schema {version} is newer than this service ({SCHEMA_VERSION})")
        if version == 0:
            with self._lock:
                self.db.executescript("BEGIN;" + _SCHEMA + f"PRAGMA user_version={SCHEMA_VERSION};COMMIT;")
            return
        for target in range(version + 1, SCHEMA_VERSION + 1):
            with self._lock:
                self.db.executescript("BEGIN;" + _MIGRATIONS[target] + f"PRAGMA user_version={target};COMMIT;")

    @contextmanager
    def tx(self):
        """One write transaction; nested use joins the outer one."""
        with self._lock:
            if self.db.in_transaction:
                yield self.db
                return
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def journal_mode(self) -> str:
        return self.db.execute("PRAGMA journal_mode").fetchone()[0]

    def writable(self) -> bool:
        try:
            with self._lock:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("ROLLBACK")
            return True
        except sqlite3.Error:
            return False

    # ------------------------------------------------------------------ generic helpers

    def one(self, sql: str, params: Iterable = ()) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.db.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def all(self, sql: str, params: Iterable = ()) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.db.execute(sql, tuple(params)).fetchall()]

    def scalar(self, sql: str, params: Iterable = ()) -> Any:
        with self._lock:
            row = self.db.execute(sql, tuple(params)).fetchone()
        return row[0] if row else None

    def execute(self, sql: str, params: Iterable = ()) -> int:
        """Rows changed."""
        with self._lock:
            return self.db.execute(sql, tuple(params)).rowcount

    @staticmethod
    def _scrub_column(column: str, value: Any) -> Any:
        if value is None:
            return None
        if column.endswith("_json") or column in JSON_COLUMNS:
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    pass
            return dumps(jsonlog.scrub(value))
        return jsonlog.redact_text(value) if isinstance(value, str) else value

    @classmethod
    def _scrubbed(cls, table: str, values: Dict[str, Any]) -> Dict[str, Any]:
        columns = SCRUBBED.get(table)
        if not columns:
            return values
        return {k: cls._scrub_column(k, v) if k in columns else v for k, v in values.items()}

    def insert(self, table: str, row: Dict[str, Any], or_ignore: bool = False) -> int:
        """Rowid of the new row."""
        row = self._scrubbed(table, row)
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        verb = "INSERT OR IGNORE" if or_ignore else "INSERT"
        with self._lock:
            return self.db.execute(f"{verb} INTO {table} ({cols}) VALUES ({marks})", tuple(row.values())).lastrowid

    def update(self, table: str, key: str, key_value: Any, values: Dict[str, Any]) -> int:
        if not values:
            return 0
        values = self._scrubbed(table, values)
        sets = ", ".join(f"{k} = ?" for k in values)
        with self._lock:
            return self.db.execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*values.values(), key_value)).rowcount

    # ------------------------------------------------------------------ rows by id

    def device(self, device_id: str) -> Dict[str, Any]:
        row = self.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        if row:
            return row
        now = timeutil.iso()
        self.execute("INSERT OR IGNORE INTO devices (device_id, created_at, updated_at) VALUES (?, ?, ?)",
                     (device_id, now, now))
        return self.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))

    def chat(self, chat_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM chats WHERE chat_id = ?", (chat_id,))

    def task(self, task_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not task_id:
            return None
        return self.one("SELECT * FROM tasks WHERE task_id = ?", (task_id,))

    def turn(self, turn_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not turn_id:
            return None
        return self.one("SELECT * FROM turns WHERE turn_id = ?", (turn_id,))

    def interaction(self, interaction_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,))

    def approval(self, approval_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM approvals WHERE approval_id = ?", (approval_id,))

    def delivery(self, delivery_id: str) -> Optional[Dict[str, Any]]:
        return self.one("SELECT * FROM deliveries WHERE delivery_id = ?", (delivery_id,))

    # ------------------------------------------------------------------ chat events

    def append_event(self, chat_id: str, type_: str, data: Dict[str, Any], ts: str, task_id: Optional[str],
                     turn_id: Optional[str], trace_id: str) -> int:
        with self.tx() as db:
            seq = db.execute("SELECT last_seq FROM chats WHERE chat_id = ?", (chat_id,)).fetchone()[0] + 1
            db.execute(
                "INSERT INTO chat_events (chat_id, seq, ts, type, task_id, turn_id, trace_id, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (chat_id, seq, ts, type_, task_id, turn_id, trace_id, dumps(data)))
            db.execute("UPDATE chats SET last_seq = ? WHERE chat_id = ?", (seq, chat_id))
        return seq

    def events_after(self, chat_id: str, after: int) -> List[Dict[str, Any]]:
        return self.all("SELECT * FROM chat_events WHERE chat_id = ? AND seq > ? ORDER BY seq", (chat_id, after))

    # ------------------------------------------------------------------ monitor data

    def record_usage(self, row: Dict[str, Any]) -> None:
        day = row["ts"][:10]
        with self.tx() as db:
            cols = ", ".join(row)
            db.execute(f"INSERT INTO usage ({cols}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values()))
            db.execute(
                """INSERT INTO usage_daily (day, model, turn_kind, results, input_tokens, output_tokens,
                     cache_read_tokens, cache_creation_tokens, web_search_requests, cost_usd)
                   VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day, model, turn_kind) DO UPDATE SET
                     results = results + 1,
                     input_tokens = input_tokens + excluded.input_tokens,
                     output_tokens = output_tokens + excluded.output_tokens,
                     cache_read_tokens = cache_read_tokens + excluded.cache_read_tokens,
                     cache_creation_tokens = cache_creation_tokens + excluded.cache_creation_tokens,
                     web_search_requests = web_search_requests + excluded.web_search_requests,
                     cost_usd = cost_usd + excluded.cost_usd""",
                (day, row["model"], row["turn_kind"], row.get("input_tokens", 0), row.get("output_tokens", 0),
                 row.get("cache_read_tokens", 0), row.get("cache_creation_tokens", 0),
                 row.get("web_search_requests", 0), row.get("cost_usd", 0.0)),
            )

    def record_error(self, source: str, code: str, message: Optional[str] = None, **ids: Any) -> None:
        self.insert("errors", {
            "ts": timeutil.iso(), "source": source, "code": code, "message": message,
            "task_id": ids.get("task_id"), "chat_id": ids.get("chat_id"), "turn_id": ids.get("turn_id"),
            "trace_id": ids.get("trace_id"),
        })

    def record_connection(self, kind: str, device_id: Optional[str], detail: Optional[Dict[str, Any]] = None) -> None:
        self.insert("connection_events", {"ts": timeutil.iso(), "device_id": device_id, "kind": kind,
                                          "detail_json": detail or {}})

    def record_probe(self, probe: str, ok: bool, latency_ms: Optional[int], detail: Dict[str, Any]) -> None:
        self.insert("health_probes", {"ts": timeutil.iso(), "probe": probe, "ok": int(ok), "latency_ms": latency_ms,
                                      "detail_json": detail})

    def replace_home_snapshot(self, rows: List[Dict[str, Any]], period_s: float) -> int:
        """Makes home_snapshot equal `rows`; only new or changed rows are written. Returns the rows written."""
        now = timeutil.iso()
        fields = ("name", "state", "attributes_json", "last_changed")
        written = 0
        with self.tx() as db:
            known = {r["entity_id"]: dict(r) for r in db.execute(
                "SELECT entity_id, name, state, attributes_json, last_changed FROM home_snapshot")}
            for row in rows:
                values = self._scrubbed("home_snapshot", {k: row.get(k) for k in fields})
                old = known.pop(row["entity_id"], None)
                if old is not None and all(old[k] == values[k] for k in fields):
                    continue
                db.execute(
                    "INSERT INTO home_snapshot (entity_id, name, state, attributes_json, last_changed, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(entity_id) DO UPDATE SET name = excluded.name, "
                    "state = excluded.state, attributes_json = excluded.attributes_json, "
                    "last_changed = excluded.last_changed, updated_at = excluded.updated_at",
                    (row["entity_id"], values["name"], values["state"], values["attributes_json"],
                     values["last_changed"], now))
                written += 1
            for entity_id in known:
                db.execute("DELETE FROM home_snapshot WHERE entity_id = ?", (entity_id,))
        return written

    def record_home_attempt(self, period_s: float, error_code: Optional[str], entity_count: int) -> None:
        """One home_snapshot_meta row: fetched_at and entity_count move only on a successful cycle."""
        now = timeutil.iso()
        ok = error_code is None
        self.execute(
            "INSERT INTO home_snapshot_meta (id, period_s, attempted_at, fetched_at, entity_count, error_code) "
            "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET period_s = excluded.period_s, "
            "attempted_at = excluded.attempted_at, error_code = excluded.error_code, "
            "fetched_at = CASE WHEN ? THEN excluded.fetched_at ELSE fetched_at END, "
            "entity_count = CASE WHEN ? THEN excluded.entity_count ELSE entity_count END",
            (period_s, now, now if ok else None, entity_count if ok else 0, error_code, int(ok), int(ok)))

    def prune(self, days: int) -> Dict[str, int]:
        cutoff = timeutil.iso(timeutil.now() - timedelta(days=days))
        removed = {}
        for table, column in RETENTION_TABLES.items():
            removed[table] = self.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
        removed["approvals"] = self.execute(
            "DELETE FROM approvals WHERE state IN ('executed', 'failed', 'not_sent', 'denied', 'expired') "
            "AND updated_at < ?", (cutoff,))
        removed["idempotency"] = self.execute(
            "DELETE FROM idempotency WHERE created_at < ?", (timeutil.iso(timeutil.now() - timedelta(days=1)),))
        return removed
