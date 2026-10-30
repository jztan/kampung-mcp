"""Housekeeping: expire mail, end long-idle sessions.

It runs before every read, and on a timer so mail on a quiet server does not
outlive its TTL.
"""

import sqlite3
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import db
from .clock import ts
from .limits import ENDED_SESSION_RETENTION, SESSION_DELETE_BATCH, TARGET_FALLBACK


def sweep(conn: sqlite3.Connection, now: datetime) -> None:
    # A reply can outlive the message it answers; drop the dangling reference
    # first so the foreign key on reply_to does not block the delete.
    conn.execute(
        "UPDATE messages SET reply_to = NULL WHERE reply_to IN "
        "(SELECT id FROM messages WHERE expires_at <= ?)",
        (ts(now),),
    )
    conn.execute("DELETE FROM messages WHERE expires_at <= ?", (ts(now),))
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL AND last_seen < ?",
        (ts(now), ts(now - TARGET_FALLBACK)),
    )
    # Drop sessions that ended long ago, freeing their suffixes. Mail still
    # pointing at one keeps it. The newest row always stays: SQLite hands out
    # max(id) + 1, so keeping it means an old session file can never meet a
    # new session under its id. A batch at a time, so a backlog cannot hold
    # the write lock for long; the next sweep takes the next batch.
    conn.execute(
        "DELETE FROM sessions WHERE id IN (SELECT id FROM sessions s "
        "WHERE s.ended_at IS NOT NULL AND s.ended_at < ? "
        "AND s.id < (SELECT max(id) FROM sessions) "
        "AND NOT EXISTS (SELECT 1 FROM messages WHERE from_session_id = s.id) "
        "AND NOT EXISTS (SELECT 1 FROM messages WHERE to_session_id = s.id) "
        "AND NOT EXISTS (SELECT 1 FROM messages WHERE delivered_to = s.id) "
        "LIMIT ?)",
        (ts(now - ENDED_SESSION_RETENTION), SESSION_DELETE_BATCH),
    )


def start_sweeper(
    db_path: Path,
    clock: Callable[[], datetime],
    interval: float,
    stop: threading.Event,
) -> threading.Thread:
    """Sweep every `interval` seconds on a daemon thread until `stop` is set."""

    def run() -> None:
        while not stop.wait(interval):
            try:
                conn = db.connect(db_path)
                try:
                    with db.transaction(conn):
                        sweep(conn, clock())
                finally:
                    conn.close()
            except Exception as e:  # keep sweeping; the next pass may work
                print(f"kampung: sweep failed: {e}", file=sys.stderr)

    thread = threading.Thread(target=run, name="kampung-sweeper", daemon=True)
    thread.start()
    return thread
