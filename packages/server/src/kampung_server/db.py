"""SQLite connections, schema migration and explicit transactions."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

# schema.sql builds version 1; schema_<n>.sql upgrades version n - 1 to n.
SCHEMA_VERSION = 2


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return
    with transaction(conn):
        # Read again under the write lock: another process may have migrated.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for statement in _statements(_script(target)):
                conn.execute(statement)
        conn.execute(f"PRAGMA user_version={max(version, SCHEMA_VERSION)}")


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
