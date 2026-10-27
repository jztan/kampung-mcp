"""Housekeeping run before reads: expire mail, end long-idle sessions."""

import sqlite3
from datetime import datetime

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
