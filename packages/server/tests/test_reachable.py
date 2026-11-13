"""Reachable sessions: list_peers and send say when an idle-wake session has
stopped polling (its machine is probably asleep), instead of calling it live."""

import sqlite3

from kampung_server.mail import list_peers
from kampung_server.models import load_machine
from server_testlib import FakeClock, MakeSession


def test_list_peers_marks_a_quiet_poller_unreachable(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("desktop", "blog", idle_wake=True)
    make_session("desktop", "blog", agent="codex")
    clock.advance(minutes=6)
    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    peers = list_peers(conn, laptop, clock.now(), "desktop")
    got = sorted(
        (s["agent"], s["reachable"], s["live"], s["seen"])
        for s in peers[0]["sessions"]  # type: ignore[attr-defined]
    )
    assert got == [
        ("claude-code", False, True, "6 min ago"),
        ("codex", None, True, "6 min ago"),
    ]
