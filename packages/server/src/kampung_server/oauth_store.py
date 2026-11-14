"""SQLite storage for the Claude app connector: one-time connect codes, pending
authorize requests, grants and tokens.

Codes and tokens are stored only as hashes. Callers run writes inside their
own `db.transaction`. This module knows nothing about OAuth or HTTP.
"""

import secrets
import sqlite3
from datetime import datetime

from . import audit
from .clock import parse_ts, ts
from .keys import hash_key
from .limits import (
    OAUTH_CONNECT_ATTEMPTS,
    OAUTH_CONNECT_CODE_TTL,
    OAUTH_MAX_REQUESTS,
    OAUTH_REQUEST_TTL,
    OAUTH_REQUESTS_PER_CLIENT,
    OAUTH_ROTATED_RETENTION,
)

KINDS = ("code", "access", "refresh")


def new_connect_code(conn: sqlite3.Connection, machine_id: int, now: datetime) -> str:
    """A fresh code for the machine; its earlier unused codes stop working."""
    conn.execute(
        "DELETE FROM oauth_connect_codes WHERE machine_id = ? AND used_at IS NULL",
        (machine_id,),
    )
    code = secrets.token_urlsafe(16)
    conn.execute(
        "INSERT INTO oauth_connect_codes (machine_id, code_hash, expires_at) "
        "VALUES (?, ?, ?)",
        (machine_id, hash_key(code), ts(now + OAUTH_CONNECT_CODE_TTL)),
    )
    return code


def take_connect_code(conn: sqlite3.Connection, code: str, now: datetime) -> int | None:
    """The machine the code was made for, marking it used in the same statement."""
    row = conn.execute(
        "UPDATE oauth_connect_codes SET used_at = ? "
        "WHERE code_hash = ? AND used_at IS NULL AND expires_at > ? "
        "RETURNING machine_id",
        (ts(now), hash_key(code), ts(now)),
    ).fetchone()
    return int(row[0]) if row else None


def save_request(
    conn: sqlite3.Connection,
    request_id: str,
    params_json: str,
    now: datetime,
    client: str,
) -> None:
    """Save a pending sign-in. An address keeps only its newest
    OAUTH_REQUESTS_PER_CLIENT; at OAUTH_MAX_REQUESTS overall the oldest goes.
    A sign-in is never refused, so a flood cannot lock the owner out: it can
    only push out requests older than the owner's, one at a time."""
    conn.execute(
        "DELETE FROM oauth_requests WHERE client = ? AND request_id NOT IN "
        "(SELECT request_id FROM oauth_requests WHERE client = ? "
        "AND expires_at > ? ORDER BY expires_at DESC, rowid DESC LIMIT ?)",
        (client, client, ts(now), OAUTH_REQUESTS_PER_CLIENT - 1),
    )
    conn.execute("DELETE FROM oauth_requests WHERE expires_at <= ?", (ts(now),))
    conn.execute(
        "DELETE FROM oauth_requests WHERE rowid IN (SELECT rowid FROM oauth_requests "
        "ORDER BY expires_at, rowid LIMIT max(0, "
        "(SELECT count(*) FROM oauth_requests) - ?))",
        (OAUTH_MAX_REQUESTS - 1,),
    )
    conn.execute(
        "INSERT INTO oauth_requests (request_id, params_json, expires_at, client) "
        "VALUES (?, ?, ?, ?)",
        (request_id, params_json, ts(now + OAUTH_REQUEST_TTL), client),
    )


def load_request(
    conn: sqlite3.Connection, request_id: str, now: datetime
) -> str | None:
    row = conn.execute(
        "SELECT params_json FROM oauth_requests "
        "WHERE request_id = ? AND expires_at > ?",
        (request_id, ts(now)),
    ).fetchone()
    return str(row[0]) if row else None


def fail_request(conn: sqlite3.Connection, request_id: str) -> int:
    """Count a wrong code; the request is deleted at OAUTH_CONNECT_ATTEMPTS."""
    row = conn.execute(
        "UPDATE oauth_requests SET attempts = attempts + 1 WHERE request_id = ? "
        "RETURNING attempts",
        (request_id,),
    ).fetchone()
    attempts = int(row[0]) if row else 0
    if attempts >= OAUTH_CONNECT_ATTEMPTS:
        conn.execute("DELETE FROM oauth_requests WHERE request_id = ?", (request_id,))
    return attempts


def take_request(
    conn: sqlite3.Connection, request_id: str, now: datetime
) -> str | None:
    row = conn.execute(
        "DELETE FROM oauth_requests WHERE request_id = ? AND expires_at > ? "
        "RETURNING params_json",
        (request_id, ts(now)),
    ).fetchone()
    return str(row[0]) if row else None


OWNER_LOST_WORDS = {
    "reconnect": "the Claude app was reconnected",
    "token_reuse": "an old sign-in token was reused, so the server dropped the "
    "connection",
    "app_revoke": "the Claude app disconnected",
    "disconnect": "the connection was removed on the server",
    "key_rotate": "the machine key was rotated",
    "revoke": "the machine was revoked",
}


def owner_lost_words(reason: str) -> str:
    return OWNER_LOST_WORDS.get(reason, reason)


def note_owner_lost(
    conn: sqlite3.Connection, machine_id: int, reason: str, now: datetime
) -> None:
    """An owner grant went: keep when and why, so the owner is told."""
    row = conn.execute(
        "UPDATE machines SET owner_lost_at = ?, owner_lost_reason = ? "
        "WHERE id = ? RETURNING name",
        (ts(now), reason, machine_id),
    ).fetchone()
    if row is not None:
        audit.record(
            conn, now, "server", "owner_cleared", {"machine": row[0], "reason": reason}
        )


def owner_lost(conn: sqlite3.Connection, machine_id: int) -> tuple[str, str] | None:
    """When and why the machine's owner mark was lost, if it still is."""
    row = conn.execute(
        "SELECT owner_lost_at, owner_lost_reason FROM machines WHERE id = ?",
        (machine_id,),
    ).fetchone()
    return (str(row[0]), str(row[1])) if row and row[0] else None


def owner_lost_at_text(at: str) -> str:
    return parse_ts(at).strftime("%Y-%m-%d %H:%M")


def lost_marks(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """(machine, lost at, reason) for every machine with a lost owner mark."""
    rows = conn.execute(
        "SELECT name, owner_lost_at, owner_lost_reason FROM machines "
        "WHERE owner_lost_at IS NOT NULL ORDER BY name"
    ).fetchall()
    return [(str(r[0]), str(r[1]), str(r[2])) for r in rows]


def clear_owner_lost(conn: sqlite3.Connection, machine_id: int) -> bool:
    cur = conn.execute(
        "UPDATE machines SET owner_lost_at = NULL, owner_lost_reason = NULL "
        "WHERE id = ? AND owner_lost_at IS NOT NULL",
        (machine_id,),
    )
    return cur.rowcount > 0


def replace_grant(conn: sqlite3.Connection, machine_id: int, now: datetime) -> int:
    """One grant per machine: the old one and its tokens go."""
    old = conn.execute(
        "DELETE FROM oauth_grants WHERE machine_id = ? RETURNING owner", (machine_id,)
    ).fetchone()
    if old is not None and old[0]:
        note_owner_lost(conn, machine_id, "reconnect", now)
    cur = conn.execute(
        "INSERT INTO oauth_grants (machine_id, created_at) VALUES (?, ?)",
        (machine_id, ts(now)),
    )
    assert cur.lastrowid is not None
    return cur.lastrowid


def grant_for_machine(conn: sqlite3.Connection, machine_id: int) -> int | None:
    row = conn.execute(
        "SELECT id FROM oauth_grants WHERE machine_id = ?", (machine_id,)
    ).fetchone()
    return int(row[0]) if row else None


def is_owner(conn: sqlite3.Connection, machine_id: int) -> bool:
    """Whether the machine's Claude app grant may send owner instructions."""
    row = conn.execute(
        "SELECT owner FROM oauth_grants WHERE machine_id = ?", (machine_id,)
    ).fetchone()
    return bool(row and row[0])


def set_owner(conn: sqlite3.Connection, machine_id: int, owner: bool) -> str | None:
    """Set or clear the owner flag on the machine's grant. Returns the grant's
    created_at, or None when the machine has no grant."""
    row = conn.execute(
        "UPDATE oauth_grants SET owner = ? WHERE machine_id = ? RETURNING created_at",
        (int(owner), machine_id),
    ).fetchone()
    return str(row[0]) if row else None


def owner_grants(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """(machine name, grant created_at) for every owner grant, by name."""
    rows = conn.execute(
        "SELECT m.name, g.created_at FROM oauth_grants g "
        "JOIN machines m ON m.id = g.machine_id WHERE g.owner = 1 ORDER BY m.name"
    ).fetchall()
    return [(str(r[0]), str(r[1])) for r in rows]


def delete_grant(
    conn: sqlite3.Connection, grant_id: int, now: datetime, reason: str
) -> None:
    """Drop the grant and end the connector session that used it."""
    row = conn.execute(
        "DELETE FROM oauth_grants WHERE id = ? RETURNING machine_id, owner",
        (grant_id,),
    ).fetchone()
    if row is not None:
        if row[1]:
            note_owner_lost(conn, int(row[0]), reason, now)
        _end_connector_session(conn, int(row[0]), now)


def delete_grant_for_machine(
    conn: sqlite3.Connection, machine_id: int, now: datetime, reason: str | None
) -> bool:
    """Drop the machine's grant. reason None: the operator removed it on
    purpose (disconnect, revoke), so a dropped owner mark is not a loss."""
    row = conn.execute(
        "DELETE FROM oauth_grants WHERE machine_id = ? RETURNING owner", (machine_id,)
    ).fetchone()
    if row is None:
        return False
    if row[0] and reason is not None:
        note_owner_lost(conn, machine_id, reason, now)
    _end_connector_session(conn, machine_id, now)
    return True


def _end_connector_session(
    conn: sqlite3.Connection, machine_id: int, now: datetime
) -> None:
    # The connector session is the one with agent claude-app in the machine's
    # mailboxes; without a grant nothing can act as it, so it ends now rather
    # than being listed live for another 30 minutes.
    conn.execute(
        "UPDATE sessions SET ended_at = ? WHERE ended_at IS NULL "
        "AND agent = 'claude-app' "
        "AND mailbox_id IN (SELECT id FROM mailboxes WHERE machine_id = ?)",
        (ts(now), machine_id),
    )


def save_token(
    conn: sqlite3.Connection,
    grant_id: int,
    kind: str,
    token: str,
    record_json: str,
    expires_at: datetime,
) -> None:
    assert kind in KINDS
    conn.execute(
        "INSERT INTO oauth_tokens (grant_id, kind, token_hash, record_json, "
        "expires_at) VALUES (?, ?, ?, ?, ?)",
        (grant_id, kind, hash_key(token), record_json, ts(expires_at)),
    )


def load_token(
    conn: sqlite3.Connection, kind: str, token: str, now: datetime
) -> tuple[int, str, str | None] | None:
    """(grant id, record, used_at) for an unexpired token, used or not."""
    row = conn.execute(
        "SELECT grant_id, record_json, used_at FROM oauth_tokens "
        "WHERE kind = ? AND token_hash = ? AND expires_at > ?",
        (kind, hash_key(token), ts(now)),
    ).fetchone()
    return (int(row[0]), str(row[1]), row[2]) if row else None


def take_token(
    conn: sqlite3.Connection, kind: str, token: str, now: datetime
) -> tuple[int, str] | None:
    """Mark a token used, once. None when it was used, expired or unknown.

    A used token stays on record for OAUTH_ROTATED_RETENTION at most, long
    enough for a reuse to be noticed, by shortening its expiry: the sweep then
    needs only the expiry index."""
    row = conn.execute(
        "UPDATE oauth_tokens SET used_at = ?, expires_at = min(expires_at, ?) "
        "WHERE kind = ? AND token_hash = ? AND used_at IS NULL AND expires_at > ? "
        "RETURNING grant_id, record_json",
        (ts(now), ts(now + OAUTH_ROTATED_RETENTION), kind, hash_key(token), ts(now)),
    ).fetchone()
    return (int(row[0]), str(row[1])) if row else None


def delete_token(conn: sqlite3.Connection, kind: str, token: str) -> None:
    conn.execute(
        "DELETE FROM oauth_tokens WHERE kind = ? AND token_hash = ?",
        (kind, hash_key(token)),
    )


def live_access_hashes(conn: sqlite3.Connection, now: datetime) -> frozenset[bytes]:
    """Access tokens, expired or not, whose grant still holds a live refresh
    token. An expired one must get the 401 that starts a refresh, never a 429."""
    rows = conn.execute(
        "SELECT a.token_hash FROM oauth_tokens a WHERE a.kind = 'access' "
        "AND EXISTS (SELECT 1 FROM oauth_tokens r WHERE r.grant_id = a.grant_id "
        "AND r.kind = 'refresh' AND r.expires_at > ?)",
        (ts(now),),
    ).fetchall()
    return frozenset(bytes(r[0]) for r in rows)


def sweep_oauth(conn: sqlite3.Connection, now: datetime) -> None:
    for table in ("oauth_connect_codes", "oauth_requests"):
        conn.execute(f"DELETE FROM {table} WHERE expires_at <= ?", (ts(now),))
    # Expired access tokens stay OAUTH_ROTATED_RETENTION longer, so a stale
    # one still gets the 401 that starts a refresh rather than a throttled 429.
    conn.execute(
        "DELETE FROM oauth_tokens WHERE kind IN ('code', 'refresh') "
        "AND expires_at <= ?",
        (ts(now),),
    )
    conn.execute(
        "DELETE FROM oauth_tokens WHERE kind = 'access' AND expires_at <= ?",
        (ts(now - OAUTH_ROTATED_RETENTION),),
    )
