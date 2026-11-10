"""The loop guard: wake budgets per pair of mailboxes and per mailbox.

A pair of mailboxes may wake each other `pair_size` times in a row; one wake
comes back every `pair_refill`. A mailbox may send `mailbox_budget` waking
messages until a human refills it (a typed prompt, the dashboard, or a
waking message from the owner's Claude app). Mail past either budget is
stored and delivered; only the wake is held, so a runaway loop stops at the
first held message.
"""

import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from .clock import parse_ts, ts


def _int_setting(env: Mapping[str, str], name: str, default: int, top: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if not 1 <= value <= top:
        raise ValueError(f"{name} must be a whole number from 1 to {top}; got {raw!r}.")
    return value


@dataclass(frozen=True)
class GuardSettings:
    pair_size: int = 9
    pair_refill: timedelta = timedelta(minutes=10)
    mailbox_budget: int = 30
    connector_cooldown: timedelta = timedelta(minutes=10)
    # Wakes the Claude app may send to one mailbox per CONNECTOR_WAKE_WINDOW.
    # The app is never woken, so it cannot be half of a loop; this only stops
    # a runaway app turn or scheduled task from waking one peer without end.
    connector_wake_budget: int = 60

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "GuardSettings":
        return cls(
            pair_size=_int_setting(env, "KAMPUNG_PAIR_WAKE_SIZE", 9, 100),
            pair_refill=timedelta(
                minutes=_int_setting(env, "KAMPUNG_PAIR_WAKE_REFILL_MINUTES", 10, 1440)
            ),
            mailbox_budget=_int_setting(env, "KAMPUNG_MAILBOX_WAKE_BUDGET", 30, 1000),
            connector_cooldown=timedelta(
                minutes=_int_setting(
                    env, "KAMPUNG_CONNECTOR_REFILL_COOLDOWN_MINUTES", 10, 1440
                )
            ),
            connector_wake_budget=_int_setting(
                env, "KAMPUNG_CONNECTOR_WAKE_BUDGET", 60, 1000
            ),
        )


def pair_key(a: int, b: int) -> tuple[int, int]:
    """The stored order of a pair of mailboxes; equal ids for a self-pair."""
    return (a, b) if a <= b else (b, a)


def pair_full_at(conn: sqlite3.Connection, a: int, b: int, now: datetime) -> datetime:
    """When the pair's bucket is full again; now if it is full already."""
    row = conn.execute(
        "SELECT full_at FROM pair_budgets WHERE mailbox_a = ? AND mailbox_b = ?",
        pair_key(a, b),
    ).fetchone()
    return now if row is None else max(now, parse_ts(row[0]))


def pair_left(
    conn: sqlite3.Connection, a: int, b: int, now: datetime, g: GuardSettings
) -> int:
    """Wakes the pair has left: one comes back every refill interval."""
    # Waiting time, never negative, so a clock stepping back mints nothing.
    waiting = (pair_full_at(conn, a, b, now) - now).total_seconds()
    owed = math.ceil(waiting / g.pair_refill.total_seconds())
    return max(0, min(g.pair_size, g.pair_size - owed))


def spend_pair(
    conn: sqlite3.Connection, a: int, b: int, now: datetime, g: GuardSettings
) -> int:
    """Take one wake from the pair; return how many are left."""
    full_at = pair_full_at(conn, a, b, now) + g.pair_refill
    lo, hi = pair_key(a, b)
    conn.execute(
        "INSERT INTO pair_budgets (mailbox_a, mailbox_b, full_at) VALUES (?, ?, ?) "
        "ON CONFLICT (mailbox_a, mailbox_b) DO UPDATE SET full_at = excluded.full_at",
        (lo, hi, ts(full_at)),
    )
    return pair_left(conn, a, b, now, g)


def mailbox_left(conn: sqlite3.Connection, mailbox_id: int, g: GuardSettings) -> int:
    used = conn.execute(
        "SELECT wakes_since_refill FROM mailboxes WHERE id = ?", (mailbox_id,)
    ).fetchone()[0]
    return max(0, g.mailbox_budget - int(used))


def spend_mailbox(conn: sqlite3.Connection, mailbox_id: int) -> None:
    conn.execute(
        "UPDATE mailboxes SET wakes_since_refill = wakes_since_refill + 1 "
        "WHERE id = ?",
        (mailbox_id,),
    )


def refill_mailbox(
    conn: sqlite3.Connection, mailbox_id: int, reachable_from: int | None = None
) -> int:
    """Refill a mailbox: its own budget and every pair it belongs to. With
    reachable_from (a machine id), only pairs whose other mailbox is on that
    machine or one paired with it. Returns the pair rows cleared."""
    conn.execute(
        "UPDATE mailboxes SET wakes_since_refill = 0 WHERE id = ?", (mailbox_id,)
    )
    other = "CASE WHEN pb.mailbox_a = :box THEN pb.mailbox_b ELSE pb.mailbox_a END"
    reach = ""
    if reachable_from is not None:
        reach = (
            f" AND (SELECT mb.machine_id FROM mailboxes mb WHERE mb.id = {other}) "
            "IN (SELECT :from UNION SELECT machine_b FROM pairings "
            "WHERE machine_a = :from UNION SELECT machine_a FROM pairings "
            "WHERE machine_b = :from)"
        )
    cur = conn.execute(
        "DELETE FROM pair_budgets WHERE rowid IN (SELECT pb.rowid FROM "
        "pair_budgets pb WHERE (pb.mailbox_a = :box OR pb.mailbox_b = :box)"
        f"{reach})",
        {"box": mailbox_id, "from": reachable_from},
    )
    return cur.rowcount


def sweep_pairs(conn: sqlite3.Connection, now: datetime) -> None:
    """Drop pairs that are full again: no row means full."""
    conn.execute("DELETE FROM pair_budgets WHERE full_at <= ?", (ts(now),))


def cap_pairs(conn: sqlite3.Connection, now: datetime, g: GuardSettings) -> None:
    """Hold every pair's debt to what the current settings allow. full_at is
    stored as a time, so a server restarted with a shorter refill interval
    would otherwise keep pairs drained for the old, longer one."""
    conn.execute(
        "UPDATE pair_budgets SET full_at = ? WHERE full_at > ?",
        (ts(now + g.pair_size * g.pair_refill),) * 2,
    )
