"""Who is calling, and who may talk to whom."""

import sqlite3

from .keys import hash_key
from .models import Machine, machine_from_row


def authenticate(conn: sqlite3.Connection, key: str) -> Machine | None:
    row = conn.execute(
        "SELECT id, name, created_at, revoked_at FROM machines "
        "WHERE key_hash = ? AND revoked_at IS NULL",
        (hash_key(key),),
    ).fetchone()
    return machine_from_row(row) if row else None


def is_paired(conn: sqlite3.Connection, a_id: int, b_id: int) -> bool:
    if a_id == b_id:
        return True
    row = conn.execute(
        "SELECT 1 FROM pairings WHERE machine_a = ? AND machine_b = ?",
        (min(a_id, b_id), max(a_id, b_id)),
    ).fetchone()
    return row is not None
