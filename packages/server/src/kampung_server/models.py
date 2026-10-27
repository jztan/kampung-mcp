"""Row types loaded from the store."""

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Machine:
    id: int
    name: str
    created_at: str
    revoked_at: str | None


def machine_from_row(row: sqlite3.Row) -> Machine:
    return Machine(row["id"], row["name"], row["created_at"], row["revoked_at"])


def load_machine(conn: sqlite3.Connection, name: str) -> Machine | None:
    row = conn.execute(
        "SELECT id, name, created_at, revoked_at FROM machines WHERE name = ?",
        (name,),
    ).fetchone()
    return machine_from_row(row) if row else None
