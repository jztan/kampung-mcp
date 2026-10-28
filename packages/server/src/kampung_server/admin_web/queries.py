"""Read-only views of the store for the dashboard. Writes go through AdminService."""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Generic, TypeVar

from ..clock import ts
from ..limits import LIVE_WINDOW

LIMIT = 100
THREAD_PREVIEW = 1000  # characters of each body a thread shows; Open has the rest
T = TypeVar("T")


@dataclass
class Page(Generic[T]):
    rows: list[T]
    total: int


# Everyday traffic: hidden from the audit log unless asked for. The
# Conversations page tells the same story in readable form.
ROUTINE = ("send", "inbox", "register", "revive")


@dataclass
class DetailField:
    key: str
    text: str
    href: str | None = None


@dataclass
class AuditRow:
    at: str
    actor: str
    action: str
    detail: str
    fields: list[DetailField]


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
    notable: bool = False,
) -> Page[AuditRow]:
    """Newest first. `notable` leaves out the ROUTINE actions."""
    p: dict[str, object] = {"actor": actor, "action": action}
    where = (
        "(:actor IS NULL OR actor = :actor) AND (:action IS NULL OR action = :action)"
    )
    if notable:
        where += " AND action NOT IN (" + ", ".join(f"'{a}'" for a in ROUTINE) + ")"
    rows = conn.execute(
        f"SELECT at, actor, action, detail FROM audit WHERE {where} "
        f"ORDER BY id DESC LIMIT {int(limit)}",
        p,
    ).fetchall()
    details = [_detail(r["detail"]) for r in rows]
    names = _session_addresses(conn, details)
    return Page(
        [
            AuditRow(r["at"], r["actor"], r["action"], r["detail"], _fields(d, names))
            for r, d in zip(rows, details)
        ],
        _count(conn, f"SELECT COUNT(*) FROM audit WHERE {where}", p),
    )


def _detail(raw: str) -> dict[str, object]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {"detail": raw}
    return data if isinstance(data, dict) else {"detail": raw}


def _session_addresses(
    conn: sqlite3.Connection, details: list[dict[str, object]]
) -> dict[int, str]:
    ids = sorted({s for d in details if isinstance(s := d.get("session"), int)})
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT s.id, {_SESSION_ADDRESS} AS address FROM sessions s "
        "JOIN mailboxes mb ON mb.id = s.mailbox_id "
        f"JOIN machines m ON m.id = mb.machine_id WHERE s.id IN ({marks})",
        ids,
    ).fetchall()
    return {r["id"]: r["address"] for r in rows}


def _fields(detail: dict[str, object], sessions: dict[int, str]) -> list[DetailField]:
    """An audit detail as labelled values: sessions by address, messages linked."""
    out: list[DetailField] = []
    for key, value in detail.items():
        values = value if isinstance(value, list) else [value]
        if key == "session" and isinstance(value, int):
            out.append(DetailField(key, sessions.get(value, f"session {value}")))
        elif key in ("id", "ids"):
            out += [DetailField(key, str(v), f"/admin/messages/{v}") for v in values]
        else:
            out.append(DetailField(key, ", ".join(str(v) for v in values)))
    return out


@dataclass
class ConversationRow:
    a: str
    b: str
    messages: int
    queued: int
    last_at: str


@dataclass
class ThreadMessage:
    id: str
    sent_at: str
    sender: str
    to: str
    depth: int
    state: str
    body: str  # the first THREAD_PREVIEW characters
    cut: int  # characters left out of `body`
    side: str  # "a" or "b": which mailbox of the pair sent it


_FROM_BOX = "fm.name || '/' || fmb.repo"
_TO_BOX = "tm.name || '/' || tmb.repo"


def conversations(conn: sqlite3.Connection, now: datetime) -> list[ConversationRow]:
    """Mailbox pairs that have exchanged mail still in the store, newest first.

    Each pair is named in sorted order, whichever side wrote first.
    """
    rows = conn.execute(
        "SELECT MIN(fa, ta) AS a, MAX(fa, ta) AS b, COUNT(*) AS n, "
        "SUM(queued) AS queued, MAX(sent_at) AS last_at FROM ("
        f"SELECT {_FROM_BOX} AS fa, {_TO_BOX} AS ta, msg.sent_at, "
        f"CASE WHEN {_QUEUED} THEN 1 ELSE 0 END AS queued {_MSG_FROM}) "
        f"GROUP BY a, b ORDER BY last_at DESC LIMIT {LIMIT}",
        {"now": ts(now)},
    ).fetchall()
    return [
        ConversationRow(r["a"], r["b"], r["n"], r["queued"], r["last_at"]) for r in rows
    ]


def thread(
    conn: sqlite3.Connection, now: datetime, a: str, b: str
) -> Page[ThreadMessage]:
    """The newest messages between two mailboxes, in either direction, shown
    oldest first."""
    p: dict[str, object] = {"now": ts(now), "a": a, "b": b}
    where = (
        f"(({_FROM_BOX} = :a AND {_TO_BOX} = :b) "
        f"OR ({_FROM_BOX} = :b AND {_TO_BOX} = :a))"
    )
    rows = conn.execute(
        f"SELECT msg.id, msg.sent_at, msg.depth, msg.body, {_FROM_BOX} AS from_box, "
        f"{_MSG_SENDER} AS sender, {_MSG_TO} AS recipient, {_MSG_STATE} AS state "
        f"{_MSG_FROM} WHERE {where} "
        f"ORDER BY msg.sent_at DESC, msg.id DESC LIMIT {LIMIT}",
        p,
    ).fetchall()
    return Page(
        [
            ThreadMessage(
                r["id"],
                r["sent_at"],
                r["sender"],
                r["recipient"],
                r["depth"],
                r["state"],
                r["body"][:THREAD_PREVIEW],
                max(0, len(r["body"]) - THREAD_PREVIEW),
                "a" if r["from_box"] == a else "b",
            )
            for r in reversed(rows)
        ],
        _count(conn, f"SELECT COUNT(*) {_MSG_FROM} WHERE {where}", p),
    )


def actors_and_actions(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """The values the audit filters offer."""
    actors = [r[0] for r in conn.execute("SELECT DISTINCT actor FROM audit ORDER BY 1")]
    actions = [
        r[0] for r in conn.execute("SELECT DISTINCT action FROM audit ORDER BY 1")
    ]
    return actors, actions
