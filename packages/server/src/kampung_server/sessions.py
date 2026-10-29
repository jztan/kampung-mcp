"""Agent sessions: registration, revival, resolution and liveness."""

import sqlite3
from datetime import datetime

from . import audit, db
from .addressing import validate_name
from .clock import parse_ts, ts
from .errors import KampungError
from .ids import new_suffix
from .limits import (
    LIVE_WINDOW,
    MAX_AGENT_SESSION_ID_CHARS,
    MAX_CWD_CHARS,
    MAX_LIVE_SESSIONS_PER_MACHINE,
    MAX_LIVE_SESSIONS_PER_MAILBOX,
    MAX_MAILBOXES_PER_MACHINE,
)
from .models import SESSION_SELECT, Machine, Session, load_session, session_from_row

AGENTS = ("claude-code", "codex", "other")


def register(
    conn: sqlite3.Connection,
    machine: Machine,
    now: datetime,
    *,
    agent: str,
    cwd: str,
    repo: str,
    hooks: bool,
    agent_session_id: str | None = None,
    suffix: str | None = None,
) -> Session:
    if agent not in AGENTS:
        raise KampungError(
            "invalid_agent", f"agent must be one of {', '.join(AGENTS)}."
        )
    validate_name("repo", repo)
    # cwd is shown to peers' agents, so it stays short and on one line.
    if not _printable(cwd, MAX_CWD_CHARS):
        raise KampungError(
            "invalid_cwd",
            f"cwd must be 1 to {MAX_CWD_CHARS} printable characters.",
        )
    if agent_session_id is not None and not _printable(
        agent_session_id, MAX_AGENT_SESSION_ID_CHARS
    ):
        raise KampungError(
            "invalid_agent_session",
            f"agent_session_id must be 1 to {MAX_AGENT_SESSION_ID_CHARS} "
            "printable characters.",
        )
    actor = f"machine:{machine.name}"
    if agent_session_id is not None:
        other = _one(conn, "s.agent_session_id = ?", (agent_session_id,))
        if other and other.machine_id != machine.id:
            audit.record(conn, now, actor, "denied", {"session": other.id})
            raise KampungError("not_owner", "That session belongs to another machine.")
    with db.transaction(conn):
        existing = None
        if agent_session_id is not None:
            existing = _one(conn, "s.agent_session_id = ?", (agent_session_id,))
        if existing is None and suffix is not None:
            existing = _one(
                conn,
                "mb.machine_id = ? AND mb.repo = ? AND s.suffix = ?",
                (machine.id, repo, suffix),
            )
        if existing is not None:
            conn.execute(
                "UPDATE sessions SET ended_at = NULL, last_seen = ? WHERE id = ?",
                (ts(now), existing.id),
            )
            audit.record(
                conn,
                now,
                actor,
                "revive",
                {"session": existing.id, "mailbox": existing.mailbox},
            )
            session_id = existing.id
        else:
            session_id = _create(
                conn, machine, now, agent, cwd, repo, hooks, agent_session_id
            )
            audit.record(
                conn,
                now,
                actor,
                "register",
                {"session": session_id, "mailbox": f"{machine.name}/{repo}"},
            )
    session = load_session(conn, session_id)
    assert session is not None
    return session


def _printable(value: str, limit: int) -> bool:
    return 0 < len(value) <= limit and value.isprintable()


def _one(
    conn: sqlite3.Connection, where: str, params: tuple[object, ...]
) -> Session | None:
    row = conn.execute(f"{SESSION_SELECT} WHERE {where}", params).fetchone()
    return session_from_row(row) if row else None


def _create(
    conn: sqlite3.Connection,
    machine: Machine,
    now: datetime,
    agent: str,
    cwd: str,
    repo: str,
    hooks: bool,
    agent_session_id: str | None,
) -> int:
    row = conn.execute(
        "SELECT id FROM mailboxes WHERE machine_id = ? AND repo = ?",
        (machine.id, repo),
    ).fetchone()
    if row is not None:
        mailbox_id = row[0]
    else:
        mailboxes = conn.execute(
            "SELECT count(*) FROM mailboxes WHERE machine_id = ?", (machine.id,)
        ).fetchone()[0]
        if mailboxes >= MAX_MAILBOXES_PER_MACHINE:
            raise KampungError(
                "too_many_mailboxes",
                f"A machine may have at most {MAX_MAILBOXES_PER_MACHINE} mailboxes.",
            )
        cur = conn.execute(
            "INSERT INTO mailboxes (machine_id, repo, created_at) VALUES (?, ?, ?)",
            (machine.id, repo, ts(now)),
        )
        mailbox_id = cur.lastrowid
    live_here, live_total = conn.execute(
        "SELECT coalesce(sum(s.mailbox_id = ?), 0), count(*) FROM sessions s "
        "JOIN mailboxes mb ON mb.id = s.mailbox_id "
        "WHERE mb.machine_id = ? AND s.ended_at IS NULL",
        (mailbox_id, machine.id),
    ).fetchone()
    if live_here >= MAX_LIVE_SESSIONS_PER_MAILBOX:
        raise KampungError(
            "too_many_sessions",
            f"{machine.name}/{repo} already has {live_here} live sessions; "
            "end some first.",
        )
    if live_total >= MAX_LIVE_SESSIONS_PER_MACHINE:
        raise KampungError(
            "too_many_sessions",
            f"{machine.name} already has {live_total} live sessions; "
            "end some first.",
        )
    taken = {
        r[0]
        for r in conn.execute(
            "SELECT suffix FROM sessions WHERE mailbox_id = ?", (mailbox_id,)
        )
    }
    cur = conn.execute(
        "INSERT INTO sessions (mailbox_id, suffix, agent, agent_session_id, hooks, "
        "cwd, started_at, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            mailbox_id,
            new_suffix(taken),
            agent,
            agent_session_id,
            int(hooks),
            cwd,
            ts(now),
            ts(now),
        ),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def resolve(
    conn: sqlite3.Connection, machine: Machine, session_id: int, now: datetime
) -> Session:
    session = load_session(conn, session_id)
    if session is None:
        raise KampungError("unknown_session", "No such session; register again.")
    if session.machine_id != machine.id:
        audit.record(
            conn, now, f"machine:{machine.name}", "denied", {"session": session_id}
        )
        raise KampungError("not_owner", "That session belongs to another machine.")
    if session.ended_at is not None:
        raise KampungError("session_ended", "Re-read your session file or re-register.")
    conn.execute(
        "UPDATE sessions SET last_seen = ? WHERE id = ?", (ts(now), session_id)
    )
    refreshed = load_session(conn, session_id)
    assert refreshed is not None
    return refreshed


def human_turn(conn: sqlite3.Connection, session: Session) -> None:
    conn.execute("UPDATE sessions SET max_depth_seen = -1 WHERE id = ?", (session.id,))


def end(conn: sqlite3.Connection, session_id: int, now: datetime) -> None:
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
        (ts(now), session_id),
    )


def is_live(session: Session, now: datetime) -> bool:
    return parse_ts(session.last_seen) >= now - LIVE_WINDOW
