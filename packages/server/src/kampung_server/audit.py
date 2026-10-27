"""Append-only audit log. Callers never pass message bodies in `detail`."""

import json
import sqlite3
from datetime import datetime

from .clock import ts


def record(
    conn: sqlite3.Connection,
    now: datetime,
    actor: str,
    action: str,
    detail: dict[str, object],
) -> None:
    conn.execute(
        "INSERT INTO audit (at, actor, action, detail) VALUES (?, ?, ?, ?)",
        (ts(now), actor, action, json.dumps(detail, sort_keys=True)),
    )
