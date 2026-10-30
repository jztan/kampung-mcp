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


@dataclass(frozen=True)
class Session:
    id: int
    mailbox_id: int
    machine_id: int
    machine: str
    repo: str
    suffix: str
    agent: str
    agent_session_id: str | None
    hooks: bool
    cwd: str
    started_at: str
    last_seen: str
    ended_at: str | None
    max_depth_seen: int
    last_delivered_at: str | None

    @property
    def mailbox(self) -> str:
        return f"{self.machine}/{self.repo}"

    @property
    def address(self) -> str:
        return f"{self.mailbox}#{self.suffix}"


SESSION_SELECT = (
    "SELECT s.id, s.mailbox_id, mb.machine_id, m.name AS machine, mb.repo, "
    "s.suffix, s.agent, s.agent_session_id, s.hooks, s.cwd, s.started_at, "
    "s.last_seen, s.ended_at, s.max_depth_seen, s.last_delivered_at "
    "FROM sessions s JOIN mailboxes mb ON mb.id = s.mailbox_id "
    "JOIN machines m ON m.id = mb.machine_id"
)


def session_from_row(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        mailbox_id=row["mailbox_id"],
        machine_id=row["machine_id"],
        machine=row["machine"],
        repo=row["repo"],
        suffix=row["suffix"],
        agent=row["agent"],
        agent_session_id=row["agent_session_id"],
        hooks=bool(row["hooks"]),
        cwd=row["cwd"],
        started_at=row["started_at"],
        last_seen=row["last_seen"],
        ended_at=row["ended_at"],
        max_depth_seen=row["max_depth_seen"],
        last_delivered_at=row["last_delivered_at"],
    )


def load_session(conn: sqlite3.Connection, session_id: int) -> Session | None:
    row = conn.execute(f"{SESSION_SELECT} WHERE s.id = ?", (session_id,)).fetchone()
    return session_from_row(row) if row else None
