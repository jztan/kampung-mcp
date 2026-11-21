"""Which messages count as waiting: shared by the dashboard and the admin
service, so a delete refuses exactly what the dashboard shows as queued."""

from datetime import datetime

from .clock import ts
from .limits import OWNER_INSTRUCTION_TTL

# An owner instruction unread past its deadline can never be read.
OWNER_EXPIRED_SQL = "(msg.owner = 1 AND msg.sent_at <= :owner_cutoff)"
QUEUED_SQL = (
    "msg.delivered_at IS NULL AND msg.expires_at > :now " f"AND NOT {OWNER_EXPIRED_SQL}"
)


def time_params(now: datetime) -> dict[str, object]:
    """The time parameters QUEUED_SQL and OWNER_EXPIRED_SQL take."""
    return {"now": ts(now), "owner_cutoff": ts(now - OWNER_INSTRUCTION_TTL)}
