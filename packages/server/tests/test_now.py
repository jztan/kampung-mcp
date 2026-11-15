"""A session's latest step ("now"): reported by its hooks, shown to peers."""

import sqlite3

import pytest

from kampung_server import db, mail, sessions
from kampung_server.clock import ts
from kampung_server.errors import KampungError
from kampung_server.models import load_machine, load_session
from server_testlib import FakeClock, MakeSession


def _now(conn: sqlite3.Connection, session_id: int) -> tuple[str | None, str | None]:
    s = load_session(conn, session_id)
    assert s is not None
    return s.activity, s.activity_at


@pytest.mark.parametrize("bad", ["two\nlines", "a\ttab", "zero​width", "x" * 81])
def test_bad_steps_are_refused_by_the_check(bad: str) -> None:
    with pytest.raises(KampungError) as e:
        sessions.check_activity(bad)
    assert e.value.code == "invalid_now"


def test_eighty_characters_fit_and_blank_is_none() -> None:
    assert sessions.check_activity("y" * 80) == "y" * 80
    assert sessions.check_activity("   ") is None
    assert sessions.check_activity("  Run the tests ") == "Run the tests"


def test_set_activity_stamps_the_time(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("laptop", "api")
    sessions.set_activity(conn, s.id, "Run the tests", clock.now())
    assert _now(conn, s.id) == ("Run the tests", ts(clock.now()))


def test_list_peers_shows_now_and_how_long_ago(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("laptop", "api")
    sessions.set_activity(conn, s.id, "editing hook.py", clock.now())
    clock.advance(minutes=4)
    me = load_machine(conn, "laptop")
    assert me is not None
    (peer,) = mail.list_peers(conn, me, clock.now())
    session = peer["sessions"][0]  # type: ignore[index]
    assert session["now"] == "editing hook.py"
    assert session["now_seen"] == "4 min ago"
    assert "summary" not in session


def test_a_session_with_no_step_shows_none(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    make_session("laptop", "api")
    me = load_machine(conn, "laptop")
    assert me is not None
    (peer,) = mail.list_peers(conn, me, clock.now())
    session = peer["sessions"][0]  # type: ignore[index]
    assert session["now"] is None and session["now_seen"] is None


def test_revive_of_an_ended_session_clears_it(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("laptop", "api", agent_session_id="cc-1")
    sessions.set_activity(conn, s.id, "old step", clock.now())
    again = make_session("laptop", "api", agent_session_id="cc-1")
    assert again.id == s.id and _now(conn, s.id)[0] == "old step"  # live: kept
    sessions.end(conn, s.id, clock.now())
    revived = make_session("laptop", "api", agent_session_id="cc-1")
    assert revived.id == s.id and _now(conn, s.id) == (None, None)


def test_migrate_12_to_13(tmp_path: object) -> None:
    from importlib import resources
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    old = db.connect(tmp_path / "old.db")
    v1 = resources.files("kampung_server").joinpath("schema.sql").read_text()
    for statement in db._statements(v1):
        old.execute(statement)
    for target in range(2, 13):
        for statement in db._statements(db._script(target)):
            old.execute(statement)
    old.execute("PRAGMA user_version=12")
    db.migrate(old)
    cols = {r[1] for r in old.execute("PRAGMA table_info(sessions)")}
    assert {"activity", "activity_at", "summary"} <= cols
