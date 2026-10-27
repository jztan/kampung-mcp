import sqlite3

import pytest

from kampung_server import db


def test_migrate_creates_tables(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r[0] for r in rows}
    assert {"machines", "pairings", "mailboxes", "sessions", "messages", "audit"} <= (
        names
    )
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_migrate_is_idempotent(conn: sqlite3.Connection) -> None:
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


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
