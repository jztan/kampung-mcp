"""Message ids and session suffixes."""

import secrets
from datetime import datetime


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
    while True:
        suffix = secrets.token_hex(2)
        if suffix not in taken:
            return suffix
