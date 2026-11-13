"""Time helpers. Stored timestamps are fixed width, so string order is time order."""

from datetime import datetime, timezone

_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, _FORMAT).replace(tzinfo=timezone.utc)


def ago(value: str | None, now: datetime) -> str:
    """'just now', '4 min ago', '3 h 05 min ago', '2 d 1 h ago'; 'never' for None."""
    if value is None:
        return "never"
    return f"{age(value, now)} ago" if age(value, now) != "0 min" else "just now"


def age(value: str, now: datetime) -> str:
    minutes = max(0, int((now - parse_ts(value)).total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} h {minutes:02d} min"
    return f"{hours // 24} d {hours % 24} h"
