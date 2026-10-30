"""Every admin write goes through here, for both the CLI and the dashboard."""

import sqlite3
from collections.abc import Callable
from datetime import datetime

from . import audit, db, sessions
from .addressing import validate_name
from .clock import ts
from .errors import KampungError
from .keys import hash_key, new_key
from .models import Machine, Session, load_machine, load_session, machine_from_row


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
                if machine.revoked_at is not None:
                    raise KampungError("already_revoked", f"{name} is already revoked.")
                conn.execute(
                    "UPDATE machines SET revoked_at = ? "
                    "WHERE id = ? AND revoked_at IS NULL",
                    (ts(now), machine.id),
                )
                _end_sessions(conn, machine, now)
                audit.record(conn, now, actor, "revoke", {"machine": name})
        finally:
            conn.close()

    def rotate_key(self, name: str, actor: str) -> str:
        """Give a machine a new key, keeping its name, mailboxes and pairings.

        The old key stops working at once, and the machine's live sessions
        end, since they were started with the old key."""
        key = new_key()
        now = self._clock()
        conn = self._connect()
        try:
            with db.transaction(conn):
                machine = _require(conn, name)
                if machine.revoked_at is not None:
                    raise KampungError("already_revoked", f"{name} is revoked.")
                conn.execute(
                    "UPDATE machines SET key_hash = ? WHERE id = ?",
                    (hash_key(key), machine.id),
                )
                _end_sessions(conn, machine, now)
                audit.record(conn, now, actor, "rotate_key", {"machine": name})
        finally:
            conn.close()
        return key

    def end_session(self, session_id: int, actor: str) -> None:
        now = self._clock()
        conn = self._connect()
        try:
            with db.transaction(conn):
                _require_open_session(conn, session_id)
                sessions.end(conn, session_id, now)
                audit.record(conn, now, actor, "end_session", {"session": session_id})
        finally:
            conn.close()

    def reset_depth(self, session_id: int, actor: str) -> None:
        now = self._clock()
        conn = self._connect()
        try:
            with db.transaction(conn):
                sessions.human_turn(conn, _require_open_session(conn, session_id))
                audit.record(conn, now, actor, "reset_depth", {"session": session_id})
        finally:
            conn.close()

    def delete_message(self, message_id: str, actor: str) -> None:
        now = self._clock()
        conn = self._connect()
        try:
            with db.transaction(conn):
                row = conn.execute(
                    "SELECT delivered_at FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
                if row is None:
                    raise KampungError("unknown_message", "No such message.")
                if row["delivered_at"] is not None:
                    raise KampungError(
                        "already_delivered",
                        "That message was already read; only queued mail can be "
                        "deleted.",
                    )
                conn.execute(
                    "UPDATE messages SET reply_to = NULL WHERE reply_to = ?",
                    (message_id,),
                )
                conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
                audit.record(conn, now, actor, "delete_message", {"id": message_id})
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


def _end_sessions(conn: sqlite3.Connection, machine: Machine, now: datetime) -> None:
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL "
        "AND mailbox_id IN (SELECT id FROM mailboxes WHERE machine_id = ?)",
        (ts(now), machine.id),
    )


def _require(conn: sqlite3.Connection, name: str) -> Machine:
    machine = load_machine(conn, name)
    if machine is None:
        raise KampungError("unknown_machine", f"No machine named {name}.")
    return machine


def _require_session(conn: sqlite3.Connection, session_id: int) -> Session:
    session = load_session(conn, session_id)
    if session is None:
        raise KampungError("unknown_session", f"No session {session_id}.")
    return session


def _require_open_session(conn: sqlite3.Connection, session_id: int) -> Session:
    session = _require_session(conn, session_id)
    if session.ended_at is not None:
        raise KampungError("session_ended", f"Session {session_id} already ended.")
    return session
