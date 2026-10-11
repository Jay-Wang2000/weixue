"""Read-only access to the agent service's SQLite database: mode=ro, query_only, one connection per thread."""
import sqlite3
import threading
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

SUPPORTED_SCHEMAS = (1, 2, 3)
BUSY_TIMEOUT_MS = 2000


class DbUnavailable(Exception):
    pass


class ReadOnlyDb:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._watch: Optional[sqlite3.Connection] = None
        self._watch_lock = threading.Lock()

    def _open(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(f"file:{quote(self.path)}?mode=ro", uri=True, isolation_level=None,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=1")
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        except sqlite3.Error as e:
            raise DbUnavailable(type(e).__name__) from None
        return conn

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._open()
        return conn

    def _drop(self) -> None:
        conn = getattr(self._local, "conn", None)
        self._local.conn = None
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass

    def query(self, sql: str, params: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        try:
            return [dict(r) for r in self._conn().execute(sql, tuple(params)).fetchall()]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e) or "no such column" in str(e):
                raise
            self._drop()
            raise DbUnavailable(type(e).__name__) from None
        except sqlite3.DatabaseError as e:
            self._drop()
            raise DbUnavailable(type(e).__name__) from None

    def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.one(sql, params)
        return next(iter(row.values())) if row else None

    def pragma(self, name: str) -> Optional[int]:
        try:
            return self._conn().execute(f"PRAGMA {name}").fetchone()[0]
        except (sqlite3.Error, DbUnavailable):
            self._drop()
            return None

    def schema_version(self) -> Optional[int]:
        return self.pragma("user_version")

    def data_version(self) -> Optional[int]:
        """Changes after another connection commits; read on one dedicated connection, since the value is
        per connection. None while the file cannot be read."""
        with self._watch_lock:
            try:
                if self._watch is None:
                    self._watch = self._open()
                return self._watch.execute("PRAGMA data_version").fetchone()[0]
            except (sqlite3.Error, DbUnavailable):
                if self._watch is not None:
                    try:
                        self._watch.close()
                    except sqlite3.Error:
                        pass
                self._watch = None
                return None

    def state(self) -> str:
        """ok | missing | schema"""
        version = self.schema_version()
        if version is None or version == 0:
            return "missing"
        return "ok" if version in SUPPORTED_SCHEMAS else "schema"

    def has_table(self, name: str) -> bool:
        try:
            return self.scalar("SELECT COUNT(*) AS n FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)) == 1
        except DbUnavailable:
            return False
