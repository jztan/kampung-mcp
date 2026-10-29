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
from .limits import TARGET_FALLBACK


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
