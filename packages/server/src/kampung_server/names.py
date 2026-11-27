"""Session names: a short human name per session, unique within its mailbox
among open sessions, so machine/repo@name always means one session.

Where a name comes from, strongest first: a rename from the owner's Claude
app (`owner_name`), then the title the session's hooks report (`title`, with
`title_kind` "env" for a name set in the agent's environment, "rename" for
Claude Code's /rename, or "auto"). A newer "rename" or "env" title replaces
an owner rename; an "auto" one never does. A name is a label: routing stays
on the session id."""

import sqlite3
from collections.abc import Iterator
from datetime import datetime

from .addressing import slug_name
from .clock import ago, ts
from .errors import KampungError
from .limits import (
    FORMER_NAME_TTL,
    FORMER_NAMES_KEPT,
    MAX_SESSION_NAME_CHARS,
    MAX_TITLE_CHARS,
)
from .models import Session, load_session
from .secret_scan import scan

KINDS = ("auto", "rename", "env")
HUMAN_KINDS = ("rename", "env")  # a person chose it, so it may undo an owner rename
MIN_PREFIX = 3
_MAX_NUMBER = 99


def check_title(raw: str) -> str | None:
    """A reported title cut to size, or None when it is blank, not one line of
    printable text, or carries a secret (peers see the name made from it)."""
    title = raw.strip()[:MAX_TITLE_CHARS]
    if not title or not title.isprintable() or scan(title) is not None:
        return None
    return title


def _candidates(want: str) -> Iterator[str]:
    """want, then want-2, want-3, ..., each cut so it still fits."""
    yield want
    for n in range(2, _MAX_NUMBER + 1):
        tail = f"-{n}"
        base = slug_name(want, MAX_SESSION_NAME_CHARS - len(tail)) or want
        yield f"{base}{tail}"


def _taken(conn: sqlite3.Connection, s: Session) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sessions WHERE mailbox_id = ? AND id != ? "
            "AND ended_at IS NULL AND name IS NOT NULL",
            (s.mailbox_id, s.id),
        )
    }


def _unique(want: str, current: str | None, taken: set[str]) -> str | None:
    """The first free form of want, except that a current name which is one of
    its forms and still free is kept, so a -2 does not flip back and forth."""
    forms = list(_candidates(want))
    if current in forms and current not in taken:
        return current
    return next((f for f in forms if f not in taken), None)


def _set_name(
    conn: sqlite3.Connection, s: Session, new: str | None, now: datetime
) -> None:
    if new == s.name:
        return
    conn.execute("UPDATE sessions SET name = ? WHERE id = ?", (new, s.id))
    if s.name is None:
        return
    conn.execute(
        "INSERT INTO session_names (session_id, name, retired_at) VALUES (?, ?, ?)",
        (s.id, s.name, ts(now)),
    )
    conn.execute(
        "DELETE FROM session_names WHERE session_id = :s AND rowid NOT IN ("
        "SELECT rowid FROM session_names WHERE session_id = :s "
        "ORDER BY retired_at DESC, rowid DESC LIMIT :keep)",
        {"s": s.id, "keep": FORMER_NAMES_KEPT},
    )


def refresh(
    conn: sqlite3.Connection, session_id: int, now: datetime, keep: bool = True
) -> str | None:
    """Work out the session's name from what it has, and store it. Without
    `keep`, a numbered name is not kept: an owner rename asked for this one."""
    s = load_session(conn, session_id)
    if s is None:
        return None
    want = s.owner_name or s.title
    current = s.name if keep else None
    new = None if want is None else _unique(want, current, _taken(conn, s))
    _set_name(conn, s, new, now)
    return new


def report_title(
    conn: sqlite3.Connection, session_id: int, kind: str, raw: str, now: datetime
) -> str | None:
    """Store the title a session's hooks reported and return its name now.
    Call inside a transaction."""
    s = load_session(conn, session_id)
    if s is None:
        return None
    if kind not in KINDS:
        kind = "auto"
    title = check_title(raw)
    slug = None if title is None else slug_name(title)
    conn.execute(
        "UPDATE sessions SET title = ?, title_kind = ? WHERE id = ?",
        (slug, kind, s.id),
    )
    if (
        s.owner_name is not None
        and kind in HUMAN_KINDS
        and slug is not None
        and slug != s.owner_title_seen
    ):
        # A person chose a name at the terminal after the owner's rename.
        conn.execute(
            "UPDATE sessions SET owner_name = NULL, owner_title_seen = NULL "
            "WHERE id = ?",
            (s.id,),
        )
    return refresh(conn, s.id, now)


def set_owner_name(
    conn: sqlite3.Connection, s: Session, name: str | None, now: datetime
) -> str | None:
    """The owner's rename (None clears it). The caller checked that the name is
    free. Remembers the /rename title current now, so only a newer one wins."""
    seen = s.title if s.title_kind in HUMAN_KINDS else None
    conn.execute(
        "UPDATE sessions SET owner_name = ?, owner_title_seen = ? WHERE id = ?",
        (name, seen if name is not None else None, s.id),
    )
    return refresh(conn, s.id, now, keep=name is None)


def holder(
    conn: sqlite3.Connection, mailbox_id: int, name: str, other_than: int
) -> str | None:
    """The suffix of the open session that has this name, if another has it."""
    row = conn.execute(
        "SELECT suffix FROM sessions WHERE mailbox_id = ? AND name = ? "
        "AND ended_at IS NULL AND id != ?",
        (mailbox_id, name, other_than),
    ).fetchone()
    return None if row is None else str(row[0])


def _listed(peer: str, rows: list[sqlite3.Row]) -> str:
    return ", ".join(f"{peer}#{r['suffix']} ({r['name']})" for r in rows)


def find(
    conn: sqlite3.Connection,
    mailbox_id: int,
    peer: str,
    name: str,
    exact: bool,
    now: datetime,
) -> str:
    """The suffix of the open session called name in this mailbox. Unless
    exact, a unique prefix of at least MIN_PREFIX characters also matches."""
    rows = conn.execute(
        "SELECT suffix, name FROM sessions WHERE mailbox_id = ? "
        "AND ended_at IS NULL AND name IS NOT NULL ORDER BY name",
        (mailbox_id,),
    ).fetchall()
    for r in rows:
        if r["name"] == name:
            return str(r["suffix"])
    if not exact and len(name) >= MIN_PREFIX:
        hits = [r for r in rows if r["name"].startswith(name)]
        if len(hits) == 1:
            return str(hits[0]["suffix"])
        if hits:
            raise KampungError(
                "ambiguous_name",
                f"Several sessions in {peer} start with {name}: "
                f"{_listed(peer, hits)}. Use the full name or #suffix.",
            )
    raise KampungError(
        "unknown_name", _unknown_hint(conn, mailbox_id, peer, name, exact, rows, now)
    )


def _unknown_hint(
    conn: sqlite3.Connection,
    mailbox_id: int,
    peer: str,
    name: str,
    exact: bool,
    rows: list[sqlite3.Row],
    now: datetime,
) -> str:
    """Where the name went, when Kampung knows: it only points, never redirects."""
    parts = [f"No open session in {peer} is called {name}."]
    since = ts(now - FORMER_NAME_TTL)
    for r in conn.execute(
        "SELECT DISTINCT s.suffix, s.name FROM session_names sn "
        "JOIN sessions s ON s.id = sn.session_id WHERE s.mailbox_id = ? "
        "AND sn.name = ? AND sn.retired_at >= ? AND s.ended_at IS NULL "
        "AND s.name IS NOT NULL",
        (mailbox_id, name, since),
    ):
        parts.append(f'"{name}" was renamed to "{r["name"]}" ({peer}#{r["suffix"]}).')
    for r in conn.execute(
        "SELECT ended_at FROM sessions WHERE mailbox_id = ? AND name = ? "
        "AND ended_at IS NOT NULL AND ended_at >= ? ORDER BY ended_at DESC LIMIT 1",
        (mailbox_id, name, since),
    ):
        parts.append(f'"{name}" ended {ago(r["ended_at"], now)}; send to {peer}.')
    if exact:
        parts.append("An owner instruction or a rename needs the exact name.")
    parts.append(
        f"Names now: {_listed(peer, rows)}."
        if rows
        else f"No session in {peer} has a name; use #suffix from list_peers."
    )
    return " ".join(parts)
