"""SQLite storage for the Claude app connector: one-time connect codes, pending
authorize requests, grants and tokens.

Codes and tokens are stored only as hashes. Callers run writes inside their
own `db.transaction`. This module knows nothing about OAuth or HTTP.
"""

import secrets
import sqlite3
from datetime import datetime

from .clock import ts
from .keys import hash_key
from .limits import (
    OAUTH_CONNECT_ATTEMPTS,
    OAUTH_CONNECT_CODE_TTL,
    OAUTH_MAX_REQUESTS,
    OAUTH_REQUEST_TTL,
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
    conn: sqlite3.Connection, request_id: str, params_json: str, now: datetime
) -> bool:
    """False when the cap on live requests is reached."""
    live = conn.execute(
        "SELECT count(*) FROM oauth_requests WHERE expires_at > ?", (ts(now),)
    ).fetchone()[0]
    if live >= OAUTH_MAX_REQUESTS:
        return False
    conn.execute(
        "INSERT INTO oauth_requests (request_id, params_json, expires_at) "
        "VALUES (?, ?, ?)",
        (request_id, params_json, ts(now + OAUTH_REQUEST_TTL)),
    )
    return True


def load_request(conn: sqlite3.Connection, request_id: str, now: datetime) -> str | None:
    row = conn.execute(
        "SELECT params_json FROM oauth_requests WHERE request_id = ? AND expires_at > ?",
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


def take_request(conn: sqlite3.Connection, request_id: str, now: datetime) -> str | None:
    row = conn.execute(
        "DELETE FROM oauth_requests WHERE request_id = ? AND expires_at > ? "
        "RETURNING params_json",
        (request_id, ts(now)),
    ).fetchone()
    return str(row[0]) if row else None


def replace_grant(conn: sqlite3.Connection, machine_id: int, now: datetime) -> int:
    """One grant per machine: the old one and its tokens go."""
    conn.execute("DELETE FROM oauth_grants WHERE machine_id = ?", (machine_id,))
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


def delete_grant(conn: sqlite3.Connection, grant_id: int) -> None:
    conn.execute("DELETE FROM oauth_grants WHERE id = ?", (grant_id,))


def delete_grant_for_machine(conn: sqlite3.Connection, machine_id: int) -> bool:
    cur = conn.execute("DELETE FROM oauth_grants WHERE machine_id = ?", (machine_id,))
    return cur.rowcount > 0


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
    """Mark a token used, once. None when it was used, expired or unknown."""
    row = conn.execute(
        "UPDATE oauth_tokens SET used_at = ? WHERE kind = ? AND token_hash = ? "
        "AND used_at IS NULL AND expires_at > ? RETURNING grant_id, record_json",
        (ts(now), kind, hash_key(token), ts(now)),
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
    for table in ("oauth_connect_codes", "oauth_requests", "oauth_tokens"):
        conn.execute(f"DELETE FROM {table} WHERE expires_at <= ?", (ts(now),))
