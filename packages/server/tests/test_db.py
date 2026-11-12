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


def test_migrates_from_version_2_to_3(tmp_path: Path) -> None:
    path = tmp_path / "v2.db"
    old = db.connect(path)
    # Build version 2 the way an installed 0.2.1 server did.
    for version in (1, 2):
        for statement in db._statements(db._script(version)):
            old.execute(statement)
    old.execute("PRAGMA user_version=2")
    old.execute(
        "INSERT INTO machines (name, key_hash, created_at) "
        "VALUES ('laptop', x'00', 't')"
    )
    db.migrate(old)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    names = {
        r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "oauth_connect_codes",
        "oauth_requests",
        "oauth_grants",
        "oauth_tokens",
    } <= names
    assert old.execute("SELECT count(*) FROM machines").fetchone()[0] == 1
    old.close()


def _version_3(path: Path) -> sqlite3.Connection:
    old = db.connect(path)
    for version in (1, 2, 3):
        for statement in db._statements(db._script(version)):
            old.execute(statement)
    old.execute("PRAGMA user_version=3")
    return old


def test_migrates_3_to_4_with_attachments_and_incremental_vacuum(
    tmp_path: Path,
) -> None:
    old = _version_3(tmp_path / "v3.db")
    old.execute(
        "INSERT INTO machines (name, key_hash, created_at) "
        "VALUES ('laptop', x'00', 't')"
    )
    db.migrate(old)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert old.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    names = {
        r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "attachments" in names
    assert old.execute("SELECT count(*) FROM machines").fetchone()[0] == 1
    old.close()


def test_connect_limits_the_wal(tmp_path: Path) -> None:
    c = db.connect(tmp_path / "x.db")
    assert c.execute("PRAGMA journal_size_limit").fetchone()[0] == 64 * 1024 * 1024
    c.close()


def _message(conn: sqlite3.Connection, message_id: str, expires_at: str) -> None:
    conn.execute(
        "INSERT INTO machines (name, key_hash, created_at) VALUES ('m', x'01', 't')"
    )
    conn.execute(
        "INSERT INTO mailboxes (machine_id, repo, created_at) VALUES (1, 'r', 't')"
    )
    conn.execute(
        "INSERT INTO sessions (mailbox_id, suffix, agent, cwd, hooks, started_at, "
        "last_seen) VALUES (1, 'aa', 'other', '/', 0, 't', 't')"
    )
    conn.execute(
        "INSERT INTO messages (id, to_mailbox_id, from_session_id, body, depth, "
        "wake, sent_at, expires_at) VALUES (?, 1, 1, 'b', 0, 1, 't', ?)",
        (message_id, expires_at),
    )


def test_attachment_goes_with_its_message(conn: sqlite3.Connection) -> None:
    _message(conn, "m1", "9999")
    conn.execute(
        "INSERT INTO attachments (message_id, name, type, bytes, data) "
        "VALUES ('m1', 'a.txt', 'text', 1, x'61')"
    )
    conn.execute("DELETE FROM messages WHERE id = 'm1'")
    assert conn.execute("SELECT count(*) FROM attachments").fetchone()[0] == 0


def test_sweep_reclaims_space_after_expiry(conn: sqlite3.Connection) -> None:
    from datetime import datetime, timezone

    from kampung_server.sweep import sweep

    _message(conn, "m1", "2000-01-01T00:00:00.000000Z")
    conn.execute(
        "INSERT INTO attachments (message_id, name, type, bytes, data) "
        "VALUES ('m1', 'a.txt', 'text', 204800, ?)",
        (b"x" * 204800,),
    )
    with db.transaction(conn):
        sweep(conn, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert conn.execute("SELECT count(*) FROM attachments").fetchone()[0] == 0
    assert conn.execute("PRAGMA freelist_count").fetchone()[0] == 0


def test_schema_5_adds_wake_budgets_and_keeps_old_depths(tmp_path: Path) -> None:
    old = db.connect(tmp_path / "v4.db")
    for version in (1, 2, 3, 4):
        for statement in db._statements(db._script(version)):
            old.execute(statement)
    old.execute("PRAGMA user_version=4")
    old.execute(
        "INSERT INTO machines (name, key_hash, created_at) VALUES ('m', x'00', 't')"
    )
    old.execute(
        "INSERT INTO mailboxes (machine_id, repo, created_at) VALUES (1, 'api', 't')"
    )
    old.execute(
        "INSERT INTO sessions (mailbox_id, suffix, agent, cwd, hooks, started_at, "
        "last_seen) VALUES (1, 'aa', 'other', '/w', 1, 't', 't')"
    )
    old.execute(
        "INSERT INTO messages (id, to_mailbox_id, from_session_id, body, depth, "
        "wake, sent_at, expires_at) VALUES ('m1', 1, 1, 'b', 4, 0, 't', 'u')"
    )
    db.migrate(old)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    msg = old.execute(
        "SELECT depth, wake_reason, spent, fyi, budget_left FROM messages"
    ).fetchone()
    assert tuple(msg) == (4, None, 0, 0, None)
    box = old.execute("SELECT wakes_since_refill, connector_refill_at FROM mailboxes")
    assert tuple(box.fetchone()) == (0, None)
    assert old.execute("SELECT count(*) FROM pair_budgets").fetchone()[0] == 0
    assert old.execute("SELECT machine_id FROM sessions").fetchone()[0] == 1
    old.close()


def test_schema_6_adds_no_reply_to_old_messages(tmp_path: Path) -> None:
    old = db.connect(tmp_path / "v5.db")
    for version in (1, 2, 3, 4, 5):
        for statement in db._statements(db._script(version)):
            old.execute(statement)
    old.execute("PRAGMA user_version=5")
    old.execute(
        "INSERT INTO machines (name, key_hash, created_at) VALUES ('m', x'00', 't')"
    )
    old.execute(
        "INSERT INTO mailboxes (machine_id, repo, created_at) VALUES (1, 'api', 't')"
    )
    old.execute(
        "INSERT INTO sessions (mailbox_id, suffix, agent, cwd, hooks, started_at, "
        "last_seen, machine_id) VALUES (1, 'aa', 'other', '/w', 1, 't', 't', 1)"
    )
    old.execute(
        "INSERT INTO messages (id, to_mailbox_id, from_session_id, body, depth, "
        "wake, sent_at, expires_at, fyi) VALUES ('m1', 1, 1, 'b', -1, 0, 't', 'u', 1)"
    )
    db.migrate(old)
    assert old.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert tuple(old.execute("SELECT fyi, no_reply FROM messages").fetchone()) == (1, 0)
    old.close()


def test_schema_7_adds_background_to_old_sessions(tmp_path: Path) -> None:
    old = db.connect(tmp_path / "v6.db")
    for version in range(1, 7):
        for statement in db._statements(db._script(version)):
            old.execute(statement)
    old.execute("PRAGMA user_version=6")
    old.execute(
        "INSERT INTO machines (name, key_hash, created_at) VALUES ('m', x'00', 't')"
    )
    old.execute(
        "INSERT INTO mailboxes (machine_id, repo, created_at) VALUES (1, 'api', 't')"
    )
    old.execute(
        "INSERT INTO sessions (mailbox_id, suffix, agent, cwd, hooks, started_at, "
        "last_seen, machine_id) VALUES (1, 'aa', 'other', '/w', 1, 't', 't', 1)"
    )
    db.migrate(old)
    assert old.execute("SELECT background FROM sessions").fetchone()[0] == 0
    old.close()


def test_schema_8_adds_idle_wake_to_old_sessions(tmp_path: Path) -> None:
    old = db.connect(tmp_path / "v7.db")
    for version in range(1, 8):
        for statement in db._statements(db._script(version)):
            old.execute(statement)
    old.execute("PRAGMA user_version=7")
    old.execute(
        "INSERT INTO machines (name, key_hash, created_at) VALUES ('m', x'00', 't')"
    )
    old.execute(
        "INSERT INTO mailboxes (machine_id, repo, created_at) VALUES (1, 'api', 't')"
    )
    old.execute(
        "INSERT INTO sessions (mailbox_id, suffix, agent, cwd, hooks, started_at, "
        "last_seen, machine_id) VALUES (1, 'aa', 'other', '/w', 1, 't', 't', 1)"
    )
    db.migrate(old)
    assert old.execute("SELECT idle_wake FROM sessions").fetchone()[0] == 0
    old.close()


def test_agent_session_id_is_unique_per_machine(conn: sqlite3.Connection) -> None:
    for name, key in (("a", "x'01'"), ("b", "x'02'")):
        conn.execute(
            f"INSERT INTO machines (name, key_hash, created_at) VALUES ('{name}', "
            f"{key}, 't')"
        )
    for machine in (1, 2):
        conn.execute(
            "INSERT INTO mailboxes (machine_id, repo, created_at) VALUES (?, 'r', 't')",
            (machine,),
        )

    def add(mailbox: int, suffix: str) -> None:
        conn.execute(
            "INSERT INTO sessions (mailbox_id, machine_id, suffix, agent, "
            "agent_session_id, cwd, hooks, started_at, last_seen) "
            "VALUES (?, ?, ?, 'other', 'cc', '/', 0, 't', 't')",
            (mailbox, mailbox, suffix),
        )

    add(1, "aa")
    add(2, "aa")
    assert [r[0] for r in conn.execute("SELECT machine_id FROM sessions")] == [1, 2]
    with pytest.raises(sqlite3.IntegrityError):
        add(1, "bb")


def _early_v5(path: Path) -> sqlite3.Connection:
    """A database migrated by develop's first schema 5, which lacked
    sessions.machine_id and oauth_requests.client."""
    old = db.connect(path)
    for version in (1, 2, 3, 4):
        for statement in db._statements(db._script(version)):
            old.execute(statement)
    folded = ("machine_id", "sessions_agent", "oauth_requests")
    for statement in db._statements(db._script(5)):
        if not any(word in statement for word in folded):
            old.execute(statement)
    old.execute("PRAGMA user_version=5")
    return old


def test_two_processes_repairing_at_once_both_succeed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    path = tmp_path / "early-v5.db"
    mine = _early_v5(path)
    real = db.transaction
    raced = []

    @contextmanager
    def racing(c: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        if not raced:
            # Another process repairs the file just before we take the lock.
            raced.append(1)
            monkeypatch.setattr(db, "transaction", real)
            other = db.connect(path)
            db.migrate(other)
            other.close()
            monkeypatch.setattr(db, "transaction", racing)
        with real(c):
            yield c

    monkeypatch.setattr(db, "transaction", racing)
    db.migrate(mine)
    names = [r[1] for r in mine.execute("PRAGMA table_info(sessions)")]
    assert names.count("machine_id") == 1
    mine.close()


def test_a_database_from_the_first_schema_5_is_repaired(tmp_path: Path) -> None:
    # Develop ran schema 5 for a few hours before sessions.machine_id and
    # oauth_requests.client were added to it; such a database says version 5
    # and would never get them.
    old = _early_v5(tmp_path / "early-v5.db")
    for name, key in (("a", "x'01'"), ("b", "x'02'")):
        old.execute(
            f"INSERT INTO machines (name, key_hash, created_at) VALUES ('{name}', "
            f"{key}, 't')"
        )
    for machine in (1, 2):
        old.execute(
            "INSERT INTO mailboxes (machine_id, repo, created_at) VALUES (?, 'r', 't')",
            (machine,),
        )
    old.execute(
        "INSERT INTO sessions (mailbox_id, suffix, agent, agent_session_id, cwd, "
        "hooks, started_at, last_seen) VALUES (2, 'aa', 'other', 'cc', '/', 0, "
        "'t', 't')"
    )
    db.migrate(old)
    assert old.execute("SELECT machine_id FROM sessions").fetchone()[0] == 2
    # The per-machine index replaced the global one: machine 1 may reuse 'cc'.
    old.execute(
        "INSERT INTO sessions (mailbox_id, machine_id, suffix, agent, "
        "agent_session_id, cwd, hooks, started_at, last_seen) "
        "VALUES (1, 1, 'aa', 'other', 'cc', '/', 0, 't', 't')"
    )
    old.execute(
        "INSERT INTO oauth_requests (request_id, params_json, expires_at, client) "
        "VALUES ('r', '{}', 'u', '192.0.2.1')"
    )
    db.migrate(old)  # a repaired database is left alone
    old.close()


def test_incremental_vacuum_is_retried_when_the_first_vacuum_failed(
    tmp_path: Path,
) -> None:
    # The version is saved before the VACUUM, so a failed VACUUM (disk full,
    # a crash) must not leave the file without incremental vacuum for good.
    conn = db.connect(tmp_path / "k.db")
    for version in range(1, db.SCHEMA_VERSION + 1):
        for statement in db._statements(db._script(version)):
            conn.execute(statement)
    conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION}")
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    db.migrate(conn)
    assert conn.execute("PRAGMA auto_vacuum").fetchone()[0] == 2
    conn.close()


def test_schema_9_adds_owner_columns(tmp_path: Path) -> None:
    old = db.connect(tmp_path / "v8.db")
    for version in range(1, 9):
        for statement in db._statements(db._script(version)):
            old.execute(statement)
    old.execute("PRAGMA user_version=8")
    old.execute(
        "INSERT INTO machines (name, key_hash, created_at) VALUES ('m', x'00', 't')"
    )
    old.execute("INSERT INTO oauth_grants (machine_id, created_at) VALUES (1, 't')")
    db.migrate(old)
    assert db.SCHEMA_VERSION == 9
    assert old.execute("PRAGMA user_version").fetchone()[0] == 9
    assert old.execute("SELECT owner FROM oauth_grants").fetchone()[0] == 0
    cols = {r[1]: r for r in old.execute("PRAGMA table_info(messages)")}
    assert cols["owner"][3] == 1 and cols["owner"][4] == "0"
    old.close()
