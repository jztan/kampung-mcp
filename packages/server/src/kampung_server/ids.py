"""Message ids, session ids and session suffixes."""

import secrets
from datetime import datetime

from .errors import KampungError


def parse_session_id(raw: str) -> int | None:
    """The session id in a header or URL path, or None. ASCII digits only:
    str.isdigit also accepts characters like "²" that int() then rejects.
    At most 18 digits, so the id fits SQLite's 64-bit integer."""
    valid = raw.isascii() and raw.isdigit() and len(raw) <= 18
    return int(raw) if valid else None


def new_message_id(now: datetime) -> str:
    """Time-ordered id: epoch milliseconds then random bits."""
    millis = int(now.timestamp() * 1000)
    return f"msg_{millis:013x}{secrets.token_hex(5)}"


def new_suffix(taken: set[str]) -> str:
    """A short session suffix not already used in the mailbox."""
    for _ in range(20):
        suffix = secrets.token_hex(1)
        if suffix not in taken:
            return suffix
    for _ in range(20):
        suffix = secrets.token_hex(2)
        if suffix not in taken:
            return suffix
    # Nearly full: scan rather than loop, since the caller holds the write lock.
    for i in range(1 << 16):
        if f"{i:04x}" not in taken:
            return f"{i:04x}"
    raise KampungError(
        "too_many_sessions", "This mailbox has used every session suffix."
    )
