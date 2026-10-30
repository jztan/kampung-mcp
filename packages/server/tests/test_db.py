import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from kampung_server import db


def test_migrate_creates_tables(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r[0] for r in rows}
    assert {"machines", "pairings", "mailboxes", "sessions", "messages", "audit"} <= (
        names
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def _indexes(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
    return {r[0] for r in rows}


def test_migrate_upgrades_a_version_1_database(tmp_path: Path) -> None:
    old = db.connect(tmp_path / "old.db")
    v1 = resources.files("kampung_server").joinpath("schema.sql").read_text()
    for statement in db._statements(v1):
        old.execute(statement)
    old.execute("PRAGMA user_version=1")
    _machine(old, "laptop")
    db.migrate(old)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert _indexes(old) >= _indexes(_fresh(tmp_path / "new.db"))
    assert old.execute("SELECT count(*) FROM machines").fetchone()[0] == 1


def _fresh(path: Path) -> sqlite3.Connection:
    c = db.connect(path)
    db.migrate(c)
    return c


def test_rows_that_get_deleted_have_indexed_references(
    conn: sqlite3.Connection,
) -> None:
    # The sweep deletes sessions and messages. SQLite checks each foreign key
    # pointing at a deleted row, and without an index on the referencing column
    # that check reads the whole table, once per deleted row.
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    indexed = set()
    for table in tables:
        for index in conn.execute(f"PRAGMA index_list({table})"):
            first = conn.execute(f"PRAGMA index_info({index['name']})").fetchone()
            indexed.add((table, first["name"]))
    missing = [
        (table, fk["from"])
        for table in tables
        for fk in conn.execute(f"PRAGMA foreign_key_list({table})")
        if fk["table"] in ("sessions", "messages")
        and (table, fk["from"]) not in indexed
    ]
    assert missing == []


def test_wal_and_foreign_keys(conn: sqlite3.Connection) -> None:
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_sessions_has_last_delivered_at(conn: sqlite3.Connection) -> None:
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)")}
    assert {"max_depth_seen", "last_delivered_at", "hooks"} <= cols


def _machine(conn: sqlite3.Connection, name: str) -> int:
    cur = conn.execute(
        "INSERT INTO machines (name, key_hash, created_at) VALUES (?, ?, 't')",
        (name, name.encode()),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def test_pairing_check_rejects_unordered(conn: sqlite3.Connection) -> None:
    a, b = _machine(conn, "laptop"), _machine(conn, "desktop")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO pairings VALUES (?, ?, 't')", (max(a, b), min(a, b)))


def test_transaction_rolls_back(conn: sqlite3.Connection) -> None:
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            _machine(conn, "laptop")
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM machines").fetchone()[0] == 0


def test_transaction_commits(conn: sqlite3.Connection) -> None:
    with db.transaction(conn):
        _machine(conn, "laptop")
    assert conn.execute("SELECT count(*) FROM machines").fetchone()[0] == 1


def test_transaction_keeps_original_error_when_already_rolled_back(
    conn: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match="original"):
        with db.transaction(conn):
            conn.execute("ROLLBACK")
            raise ValueError("original")
