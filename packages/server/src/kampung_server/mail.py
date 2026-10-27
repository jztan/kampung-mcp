"""Sending and reading mail, and the loop guard."""

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime

from . import audit, db
from .addressing import parse_address
from .clock import parse_ts, ts
from .errors import KampungError
from .identity import is_paired
from .ids import new_message_id
from .limits import MAX_BODY_BYTES, MESSAGE_TTL, NOHOOK_DEPTH_RESET, WAKE_DEPTH_LIMIT
from .models import Session, load_machine


@dataclass(frozen=True)
class SendResult:
    id: str
    depth: int
    wake: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def effective_depth(session: Session, now: datetime) -> int:
    """The sender's loop-guard depth; sessions without hooks reset on a timer."""
    if not session.hooks and (
        session.last_delivered_at is None
        or parse_ts(session.last_delivered_at) < now - NOHOOK_DEPTH_RESET
    ):
        return -1
    return session.max_depth_seen


def send(
    conn: sqlite3.Connection,
    sender: Session,
    to: str,
    body: str,
    reply_to: str | None,
    now: datetime,
) -> SendResult:
    actor = f"machine:{sender.machine}"
    if body.strip() == "":
        raise KampungError("empty_body", "The message body is empty.")
    size = len(body.encode("utf-8"))
    if size > MAX_BODY_BYTES:
        raise KampungError(
            "body_too_large",
            f"Messages are limited to {MAX_BODY_BYTES} bytes of UTF-8; "
            f"this one is {size}.",
        )
    address = parse_address(to)
    target = load_machine(conn, address.machine)
    if target is None:
        raise KampungError(
            "unknown_machine", "No such machine; call list_peers to see addresses."
        )
    if target.revoked_at is not None:
        raise KampungError(
            "machine_revoked", f"Machine {target.name} has been revoked."
        )
    if not is_paired(conn, sender.machine_id, target.id):
        audit.record(conn, now, actor, "denied", {"to": to, "reason": "not_paired"})
        raise KampungError(
            "not_paired",
            f"Ask the owner to run: kampung pair {sender.machine} {target.name}",
        )
    mailbox = conn.execute(
        "SELECT id FROM mailboxes WHERE machine_id = ? AND repo = ?",
        (target.id, address.repo),
    ).fetchone()
    if mailbox is None:
        raise KampungError("unknown_mailbox", "Call list_peers to see mailboxes.")
    to_session_id = None
    if address.suffix is not None:
        row = conn.execute(
            "SELECT id FROM sessions WHERE mailbox_id = ? AND suffix = ? "
            "AND ended_at IS NULL",
            (mailbox["id"], address.suffix),
        ).fetchone()
        if row is None:
            raise KampungError(
                "unknown_session",
                "That session has ended; send to the mailbox without #suffix.",
            )
        to_session_id = row["id"]
    if reply_to is not None:
        if not conn.execute(
            "SELECT 1 FROM messages WHERE id = ?", (reply_to,)
        ).fetchone():
            raise KampungError("unknown_message", "reply_to names no known message.")

    depth = effective_depth(sender, now) + 1
    wake = depth <= WAKE_DEPTH_LIMIT
    message_id = new_message_id(now)
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO messages (id, to_mailbox_id, to_session_id, from_session_id, "
            "body, depth, wake, reply_to, sent_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                mailbox["id"],
                to_session_id,
                sender.id,
                body,
                depth,
                int(wake),
                reply_to,
                ts(now),
                ts(now + MESSAGE_TTL),
            ),
        )
        audit.record(
            conn,
            now,
            actor,
            "send",
            {"id": message_id, "to": to, "depth": depth, "wake": wake, "bytes": size},
        )
    return SendResult(message_id, depth, wake)
