"""Every admin write goes through here, for both the CLI and the dashboard."""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from . import audit, db
from .addressing import validate_name
from .clock import ts
from .errors import KampungError
from .keys import hash_key, new_key
from .models import Machine, load_machine, machine_from_row


class AdminService:
    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        clock: Callable[[], datetime],
    ) -> None:
        self._connect = connect
        self._clock = clock

    def add_machine(self, name: str, actor: str) -> tuple[Machine, str]:
        validate_name("machine", name)
        key = new_key()
        now = self._clock()
        conn = self._connect()
        try:
            with db.transaction(conn):
                if load_machine(conn, name):
                    raise KampungError(
                        "name_taken", f"A machine named {name} already exists."
                    )
                conn.execute(
                    "INSERT INTO machines (name, key_hash, created_at) "
                    "VALUES (?, ?, ?)",
                    (name, hash_key(key), ts(now)),
                )
                audit.record(conn, now, actor, "add_machine", {"machine": name})
                machine = load_machine(conn, name)
        finally:
            conn.close()
        assert machine is not None
        return machine, key

    def pair(self, a: str, b: str, actor: str) -> None:
        self._pairing(a, b, actor, add=True)

    def unpair(self, a: str, b: str, actor: str) -> None:
        self._pairing(a, b, actor, add=False)

    def _pairing(self, a: str, b: str, actor: str, *, add: bool) -> None:
        now = self._clock()
        conn = self._connect()
        try:
            with db.transaction(conn):
                ma, mb = _require(conn, a), _require(conn, b)
                if ma.id == mb.id:
                    raise KampungError(
                        "same_machine", "A machine is always paired with itself."
                    )
                lo, hi = sorted((ma.id, mb.id))
                if add:
                    conn.execute(
                        "INSERT OR IGNORE INTO pairings VALUES (?, ?, ?)",
                        (lo, hi, ts(now)),
                    )
                else:
                    conn.execute(
                        "DELETE FROM pairings WHERE machine_a = ? AND machine_b = ?",
                        (lo, hi),
                    )
                action = "pair" if add else "unpair"
                audit.record(conn, now, actor, action, {"machines": sorted((a, b))})
        finally:
            conn.close()

    def revoke(self, name: str, actor: str) -> None:
        now = self._clock()
        conn = self._connect()
        try:
            with db.transaction(conn):
                machine = _require(conn, name)
                conn.execute(
                    "UPDATE machines SET revoked_at = ? "
                    "WHERE id = ? AND revoked_at IS NULL",
                    (ts(now), machine.id),
                )
                conn.execute(
                    "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL "
                    "AND mailbox_id IN (SELECT id FROM mailboxes WHERE machine_id = ?)",
                    (ts(now), machine.id),
                )
                audit.record(conn, now, actor, "revoke", {"machine": name})
        finally:
            conn.close()

    def list_machines(self) -> list[Machine]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, name, created_at, revoked_at FROM machines ORDER BY name"
            ).fetchall()
        finally:
            conn.close()
        return [machine_from_row(r) for r in rows]

    def list_pairs(self) -> list[tuple[str, str]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT a.name, b.name FROM pairings p "
                "JOIN machines a ON a.id = p.machine_a "
                "JOIN machines b ON b.id = p.machine_b"
            ).fetchall()
        finally:
            conn.close()
        pairs = [(min(r[0], r[1]), max(r[0], r[1])) for r in rows]
        return sorted(pairs)


def _require(conn: sqlite3.Connection, name: str) -> Machine:
    machine = load_machine(conn, name)
    if machine is None:
        raise KampungError("unknown_machine", f"No machine named {name}.")
    return machine
