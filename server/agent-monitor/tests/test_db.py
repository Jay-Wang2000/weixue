"""Read-only connection: writes fail, data_version follows another connection's commits, missing and schema states."""
import sqlite3

import pytest

from agent_monitor.db import DbUnavailable, ReadOnlyDb


def test_insert_fails(db):
    with pytest.raises((sqlite3.OperationalError, DbUnavailable)):
        db.query("INSERT INTO errors (ts, source, code) VALUES ('x', 'monitor', 'write')")
    assert db.scalar("SELECT COUNT(*) AS n FROM errors WHERE source = 'monitor'") == 0
    with pytest.raises((sqlite3.OperationalError, DbUnavailable)):
        db.query("PRAGMA query_only=0; DELETE FROM errors")


def test_data_version_changes_after_another_connection_commits(db, db_path):
    first = db.data_version()
    assert db.data_version() == first
    writer = sqlite3.connect(db_path)
    writer.execute("INSERT INTO errors (ts, source, code) VALUES ('2026-10-26T12:00:00.000Z', 'test', 'x')")
    writer.commit()
    assert db.data_version() != first
    assert db.scalar("SELECT COUNT(*) AS n FROM errors WHERE source = 'test'") == 1
    writer.close()


def test_reads_while_a_writer_holds_a_transaction(db, db_path):
    writer = sqlite3.connect(db_path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO errors (ts, source, code) VALUES ('t', 'uncommitted', 'x')")
    assert db.scalar("SELECT COUNT(*) AS n FROM errors WHERE source = 'uncommitted'") == 0
    writer.execute("ROLLBACK")
    writer.close()


def test_missing_file_and_unknown_schema(tmp_path, db_path):
    missing = ReadOnlyDb(str(tmp_path / "nope" / "agent.db"))
    assert missing.state() == "missing" and missing.data_version() is None
    with pytest.raises(DbUnavailable):
        missing.query("SELECT 1")
    assert not (tmp_path / "nope").exists()
    writer = sqlite3.connect(db_path)
    writer.execute("PRAGMA user_version=99")
    writer.commit()
    writer.close()
    assert ReadOnlyDb(str(db_path)).state() == "schema"
