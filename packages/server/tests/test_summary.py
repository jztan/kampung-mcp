"""A session's summary: set by its own agent, shown to peers."""

import sqlite3

import pytest

from kampung_server import db, mail, sessions
from kampung_server.errors import KampungError
from kampung_server.models import load_machine, load_session
from server_testlib import FakeClock, MakeSession


def _summary(conn: sqlite3.Connection, session_id: int) -> str | None:
    s = load_session(conn, session_id)
    assert s is not None
    return s.summary


def test_set_replace_and_clear(
    conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    s = make_session("laptop", "api")
    assert mail.set_summary(conn, s, "fixing the login form") == (
        "fixing the login form"
    )
    assert _summary(conn, s.id) == "fixing the login form"
    mail.set_summary(conn, s, "  writing release notes  ")
    assert _summary(conn, s.id) == "writing release notes"
    for empty in ("", "   "):
        mail.set_summary(conn, s, "x")
        assert mail.set_summary(conn, s, empty) is None
        assert _summary(conn, s.id) is None


@pytest.mark.parametrize("bad", ["two\nlines", "a\ttab", "zero​width", "x" * 81])
def test_bad_summaries_are_refused(
    conn: sqlite3.Connection, make_session: MakeSession, bad: str
) -> None:
    s = make_session("laptop", "api")
    mail.set_summary(conn, s, "before")
    with pytest.raises(KampungError) as e:
        mail.set_summary(conn, s, bad)
    assert e.value.code == "invalid_summary"
    assert e.value.hint == ("A summary is one line of up to 80 printable characters.")
    assert _summary(conn, s.id) == "before"


def test_eighty_characters_fit(
    conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    s = make_session("laptop", "api")
    assert mail.set_summary(conn, s, "y" * 80) == "y" * 80


def test_a_secret_is_refused_like_in_a_body(
    conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    s = make_session("laptop", "api")
    secret = "rotating ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    with pytest.raises(KampungError) as body:
        mail._scan(secret, None)
    with pytest.raises(KampungError) as e:
        mail.set_summary(conn, s, secret)
    assert e.value.code == body.value.code
    assert _summary(conn, s.id) is None


def test_the_claude_app_has_no_summary(
    conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    app = make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app"
    )
    with pytest.raises(KampungError) as e:
        mail.set_summary(conn, app, "chatting")
    assert (e.value.code, e.value.hint) == (
        "not_for_app",
        "The Claude app's session is shared by every chat, so it has no summary.",
    )


def test_revive_of_an_ended_session_clears_it(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("laptop", "api", agent_session_id="cc-1")
    mail.set_summary(conn, s, "old task")
    again = make_session("laptop", "api", agent_session_id="cc-1")
    assert again.id == s.id and _summary(conn, s.id) == "old task"  # live: kept
    sessions.end(conn, s.id, clock.now())
    revived = make_session("laptop", "api", agent_session_id="cc-1")
    assert revived.id == s.id and _summary(conn, s.id) is None


def test_migrate_11_to_12(tmp_path: object) -> None:
    from importlib import resources
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    old = db.connect(tmp_path / "old.db")
    v1 = resources.files("kampung_server").joinpath("schema.sql").read_text()
    for statement in db._statements(v1):
        old.execute(statement)
    for target in range(2, 12):
        for statement in db._statements(db._script(target)):
            old.execute(statement)
    old.execute("PRAGMA user_version=11")
    db.migrate(old)
    cols = {r[1] for r in old.execute("PRAGMA table_info(sessions)")}
    assert "summary" in cols
    assert load_machine is not None  # keep the import used
