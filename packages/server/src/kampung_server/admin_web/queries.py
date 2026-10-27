"""Read-only views of the store for the dashboard. Writes go through AdminService."""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, TypeVar

from ..clock import ts
from ..limits import LIVE_WINDOW

LIMIT = 100
T = TypeVar("T")


@dataclass
class Page(Generic[T]):
    rows: list[T]
    total: int


@dataclass
class AuditRow:
    at: str
    actor: str
    action: str
    detail: str


@dataclass
class Overview:
    machines: int
    revoked: int
    live_sessions: int
    queued: int
    no_wake: int
    sent_24h: int
    oldest_queued_at: str | None
    recent_audit: list[AuditRow]


@dataclass
class MachineRow:
    name: str
    active: bool
    paired_with: list[str]
    last_seen: str | None
    created_at: str


@dataclass
class MailboxRow:
    address: str
    live: int
    total_sessions: int
    queued: int
    oldest_queued_at: str | None


@dataclass
class SessionRow:
    id: int
    address: str
    agent: str
    state: str
    depth: int
    cwd: str
    started_at: str
    last_seen: str


@dataclass
class MessageRow:
    id: str
    sent_at: str
    sender: str
    to: str
    depth: int
    state: str
    size: int


@dataclass
class MessageDetail:
    id: str
    sender: str
    to: str
    sent_at: str
    expires_at: str
    depth: int
    wake: bool
    state: str
    reply_to: str | None
    delivered_to: str | None
    delivered_at: str | None
    body: str


# Queued means undelivered and unexpired; no-wake is queued mail the loop guard
# held back. Expired mail stays in the table until the sweep removes it.
_MSG_STATE = (
    "CASE WHEN msg.delivered_at IS NOT NULL THEN 'delivered' "
    "WHEN msg.expires_at <= :now THEN 'expired' "
    "WHEN msg.wake = 0 THEN 'no-wake' ELSE 'queued' END"
)
_QUEUED = "msg.delivered_at IS NULL AND msg.expires_at > :now"
_SESSION_ADDRESS = "m.name || '/' || mb.repo || '#' || s.suffix"
_MSG_FROM = (
    "FROM messages msg "
    "JOIN sessions fs ON fs.id = msg.from_session_id "
    "JOIN mailboxes fmb ON fmb.id = fs.mailbox_id "
    "JOIN machines fm ON fm.id = fmb.machine_id "
    "JOIN mailboxes tmb ON tmb.id = msg.to_mailbox_id "
    "JOIN machines tm ON tm.id = tmb.machine_id "
    "LEFT JOIN sessions ts ON ts.id = msg.to_session_id "
)
_MSG_SENDER = "fm.name || '/' || fmb.repo || '#' || fs.suffix"
_MSG_TO = "tm.name || '/' || tmb.repo || COALESCE('#' || ts.suffix, '')"


def _live_since(now: datetime) -> str:
    return ts(now - LIVE_WINDOW)


def _count(conn: sqlite3.Connection, sql: str, params: dict[str, object]) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def overview(conn: sqlite3.Connection, now: datetime) -> Overview:
    p: dict[str, object] = {"now": ts(now), "live": _live_since(now)}
    oldest = conn.execute(
        f"SELECT MIN(sent_at) FROM messages msg WHERE {_QUEUED}", p
    ).fetchone()[0]
    return Overview(
        machines=_count(conn, "SELECT COUNT(*) FROM machines", p),
        revoked=_count(
            conn, "SELECT COUNT(*) FROM machines WHERE revoked_at IS NOT NULL", p
        ),
        live_sessions=_count(
            conn,
            "SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL "
            "AND last_seen >= :live",
            p,
        ),
        queued=_count(conn, f"SELECT COUNT(*) FROM messages msg WHERE {_QUEUED}", p),
        no_wake=_count(
            conn,
            f"SELECT COUNT(*) FROM messages msg WHERE {_QUEUED} AND msg.wake = 0",
            p,
        ),
        sent_24h=_count(
            conn,
            "SELECT COUNT(*) FROM messages WHERE sent_at > :since",
            {"since": ts(now - timedelta(hours=24))},
        ),
        oldest_queued_at=oldest,
        recent_audit=audit(conn, None, None, limit=20).rows,
    )


def machines(conn: sqlite3.Connection, now: datetime) -> list[MachineRow]:
    rows = conn.execute(
        "SELECT m.id, m.name, m.revoked_at, m.created_at, "
        "(SELECT MAX(s.last_seen) FROM sessions s JOIN mailboxes mb "
        " ON mb.id = s.mailbox_id WHERE mb.machine_id = m.id) AS last_seen "
        "FROM machines m ORDER BY m.revoked_at IS NOT NULL, m.name"
    ).fetchall()
    pairs = conn.execute(
        "SELECT a.name AS a, b.name AS b FROM pairings p "
        "JOIN machines a ON a.id = p.machine_a JOIN machines b ON b.id = p.machine_b"
    ).fetchall()
    peers: dict[str, list[str]] = {}
    for r in pairs:
        peers.setdefault(r["a"], []).append(r["b"])
        peers.setdefault(r["b"], []).append(r["a"])
    return [
        MachineRow(
            name=r["name"],
            active=r["revoked_at"] is None,
            paired_with=sorted(peers.get(r["name"], [])),
            last_seen=r["last_seen"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def mailboxes(conn: sqlite3.Connection, now: datetime) -> list[MailboxRow]:
    rows = conn.execute(
        "SELECT m.name || '/' || mb.repo AS address, "
        "(SELECT COUNT(*) FROM sessions s WHERE s.mailbox_id = mb.id "
        " AND s.ended_at IS NULL AND s.last_seen >= :live) AS live, "
        "(SELECT COUNT(*) FROM sessions s WHERE s.mailbox_id = mb.id) AS total, "
        f"(SELECT COUNT(*) FROM messages msg WHERE msg.to_mailbox_id = mb.id "
        f" AND {_QUEUED}) AS queued, "
        f"(SELECT MIN(msg.sent_at) FROM messages msg WHERE msg.to_mailbox_id = mb.id "
        f" AND {_QUEUED}) AS oldest "
        "FROM mailboxes mb JOIN machines m ON m.id = mb.machine_id "
        "ORDER BY address",
        {"now": ts(now), "live": _live_since(now)},
    ).fetchall()
    return [
        MailboxRow(r["address"], r["live"], r["total"], r["queued"], r["oldest"])
        for r in rows
    ]


def sessions(
    conn: sqlite3.Connection,
    now: datetime,
    state: str = "open",
    agent: str | None = None,
) -> Page[SessionRow]:
    where = {"open": "s.ended_at IS NULL", "ended": "s.ended_at IS NOT NULL"}.get(
        state, "1"
    )
    p: dict[str, object] = {"live": _live_since(now), "agent": agent}
    cond = f"{where} AND (:agent IS NULL OR s.agent = :agent)"
    base = (
        "FROM sessions s JOIN mailboxes mb ON mb.id = s.mailbox_id "
        f"JOIN machines m ON m.id = mb.machine_id WHERE {cond}"
    )
    rows = conn.execute(
        f"SELECT s.id, {_SESSION_ADDRESS} AS address, s.agent, s.cwd, "
        "s.started_at, s.last_seen, s.max_depth_seen, "
        "CASE WHEN s.ended_at IS NOT NULL THEN 'ended' "
        "WHEN s.last_seen >= :live THEN 'live' ELSE 'idle' END AS state "
        f"{base} ORDER BY s.last_seen DESC, s.id DESC LIMIT {LIMIT}",
        p,
    ).fetchall()
    return Page(
        [
            SessionRow(
                r["id"],
                r["address"],
                r["agent"],
                r["state"],
                r["max_depth_seen"],
                r["cwd"],
                r["started_at"],
                r["last_seen"],
            )
            for r in rows
        ],
        _count(conn, f"SELECT COUNT(*) {base}", p),
    )


def messages(
    conn: sqlite3.Connection,
    now: datetime,
    mailbox: str | None,
    state: str = "queued",
    no_wake_only: bool = False,
) -> Page[MessageRow]:
    states = {
        "queued": _QUEUED,
        "delivered": "msg.delivered_at IS NOT NULL",
        "expired": "msg.delivered_at IS NULL AND msg.expires_at <= :now",
    }
    conds = [states.get(state, "1")]
    if no_wake_only:
        conds.append("msg.wake = 0")
    if mailbox:
        conds.append("tm.name || '/' || tmb.repo = :mailbox")
    p: dict[str, object] = {"now": ts(now), "mailbox": mailbox}
    where = " AND ".join(conds)
    rows = conn.execute(
        f"SELECT msg.id, msg.sent_at, msg.depth, LENGTH(CAST(msg.body AS BLOB)) "
        f"AS size, {_MSG_SENDER} AS sender, {_MSG_TO} AS recipient, "
        f"{_MSG_STATE} AS state {_MSG_FROM} WHERE {where} "
        f"ORDER BY msg.sent_at DESC, msg.id DESC LIMIT {LIMIT}",
        p,
    ).fetchall()
    return Page(
        [
            MessageRow(
                r["id"],
                r["sent_at"],
                r["sender"],
                r["recipient"],
                r["depth"],
                r["state"],
                r["size"],
            )
            for r in rows
        ],
        _count(conn, f"SELECT COUNT(*) {_MSG_FROM} WHERE {where}", p),
    )


def message(
    conn: sqlite3.Connection, now: datetime, message_id: str
) -> MessageDetail | None:
    r = conn.execute(
        f"SELECT msg.*, {_MSG_SENDER} AS sender, {_MSG_TO} AS recipient, "
        f"{_MSG_STATE} AS state, "
        "(SELECT dm.name || '/' || dmb.repo || '#' || ds.suffix FROM sessions ds "
        " JOIN mailboxes dmb ON dmb.id = ds.mailbox_id "
        " JOIN machines dm ON dm.id = dmb.machine_id "
        " WHERE ds.id = msg.delivered_to) AS delivered_address "
        f"{_MSG_FROM} WHERE msg.id = :id",
        {"now": ts(now), "id": message_id},
    ).fetchone()
    if r is None:
        return None
    return MessageDetail(
        id=r["id"],
        sender=r["sender"],
        to=r["recipient"],
        sent_at=r["sent_at"],
        expires_at=r["expires_at"],
        depth=r["depth"],
        wake=bool(r["wake"]),
        state=r["state"],
        reply_to=r["reply_to"],
        delivered_to=r["delivered_address"],
        delivered_at=r["delivered_at"],
        body=r["body"],
    )


def audit(
    conn: sqlite3.Connection,
    actor: str | None,
    action: str | None,
    limit: int = LIMIT,
) -> Page[AuditRow]:
    p: dict[str, object] = {"actor": actor, "action": action}
    where = (
        "(:actor IS NULL OR actor = :actor) AND (:action IS NULL OR action = :action)"
    )
    rows = conn.execute(
        f"SELECT at, actor, action, detail FROM audit WHERE {where} "
        f"ORDER BY id DESC LIMIT {int(limit)}",
        p,
    ).fetchall()
    return Page(
        [AuditRow(r["at"], r["actor"], r["action"], r["detail"]) for r in rows],
        _count(conn, f"SELECT COUNT(*) FROM audit WHERE {where}", p),
    )


def actors_and_actions(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """The values the audit filters offer."""
    actors = [r[0] for r in conn.execute("SELECT DISTINCT actor FROM audit ORDER BY 1")]
    actions = [
        r[0] for r in conn.execute("SELECT DISTINCT action FROM audit ORDER BY 1")
    ]
    return actors, actions
