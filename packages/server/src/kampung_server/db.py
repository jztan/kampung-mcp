"""SQLite connections, schema migration and explicit transactions."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

# schema.sql builds version 1; schema_<n>.sql upgrades version n - 1 to n.
# A schema file is frozen once it is pushed: a database at version n never
# runs schema_<n>.sql again, so a later edit never reaches it, and anyone may
# have run a pushed develop build. Changes go in a new file.
SCHEMA_VERSION = 12
# Deleted attachments give space back, and the WAL is trimmed after a
# checkpoint, so a busy week cannot leave the volume full.
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_LIMIT}")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version >= 5:
        # Before any later script, which assumes the full schema 5.
        _repair_early_schema_5(conn)
    if version >= SCHEMA_VERSION:
        _ensure_incremental_vacuum(conn)
        return
    with transaction(conn):
        # Read again under the write lock: another process may have migrated.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _statements(_script(target)):
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version={max(version, SCHEMA_VERSION)}")
    _ensure_incremental_vacuum(conn)


def _ensure_incremental_vacuum(conn: sqlite3.Connection) -> None:
    """Checked on every start, not only after a migration: the version is
    saved before the VACUUM, so a VACUUM that failed (disk full, a crash)
    is retried next time instead of leaving the file without it for good."""
    if conn.execute("PRAGMA auto_vacuum").fetchone()[0] != 2:
        # Switching an existing file to incremental vacuum needs a VACUUM,
        # which cannot run inside a transaction. A new file gets it at once.
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
        conn.execute("VACUUM")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _repair_early_schema_5(conn: sqlite3.Connection) -> None:
    """Add what schema_5.sql gained after develop first ran it. For a few hours
    on 2026-11-08 develop's schema 5 lacked sessions.machine_id (and its
    per-machine index) and oauth_requests.client; a database migrated then
    says version 5, so it would never run them. Both checks are cheap reads
    on a database that has them."""
    sessions_ok = "machine_id" in _columns(conn, "sessions")
    requests_ok = "client" in _columns(conn, "oauth_requests")
    if sessions_ok and requests_ok:
        return
    with transaction(conn):
        # Read again under the write lock: another process may have repaired
        # the file between the check above and here.
        if "machine_id" not in _columns(conn, "sessions"):
            conn.execute(
                "ALTER TABLE sessions ADD COLUMN machine_id INTEGER "
                "REFERENCES machines(id)"
            )
            conn.execute(
                "UPDATE sessions SET machine_id = (SELECT mb.machine_id FROM "
                "mailboxes mb WHERE mb.id = sessions.mailbox_id)"
            )
            conn.execute("DROP INDEX IF EXISTS sessions_agent")
            conn.execute(
                "CREATE UNIQUE INDEX sessions_agent ON sessions "
                "(machine_id, agent_session_id) WHERE agent_session_id IS NOT NULL"
            )
        if "client" not in _columns(conn, "oauth_requests"):
            conn.execute(
                "ALTER TABLE oauth_requests ADD COLUMN client TEXT NOT NULL "
                "DEFAULT ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS oauth_requests_client "
                "ON oauth_requests (client)"
            )


def _script(version: int) -> str:
    name = "schema.sql" if version == 1 else f"schema_{version}.sql"
    return resources.files("kampung_server").joinpath(name).read_text()


def _statements(script: str) -> list[str]:
    """Split a DDL script on ';' (the schema has no ';' inside strings)."""
    lines = [line.split("--", 1)[0] for line in script.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        # SQLite may already have rolled back (some errors end the transaction),
        # and a second ROLLBACK would hide the original error.
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
