"""Session names: a short human name per session, unique within its mailbox,
taken from the title the agent reports or set by the owner from the Claude app."""

import sqlite3
from datetime import timedelta
from typing import Any

import pytest

from kampung_server import db, mail, names, oauth_store, sessions
from kampung_server.addressing import parse_address, slug_name
from kampung_server.errors import KampungError
from kampung_server.mail import send
from kampung_server.models import Session, load_machine, load_session
from server_testlib import FakeClock, MakeSession

# -- the name itself


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Stale mailbox removal schedule", "stale-mailbox-removal-schedule"),
        ("  Fix: the CI (Windows)!  ", "fix-the-ci-windows"),
        ("Café work", "cafe-work"),
        ("gateway-work", "gateway-work"),
        ("Review the code changes in this repo since the release", None),
        ("!!!", None),
        ("", None),
    ],
)
def test_slug_name(title: str, slug: str | None) -> None:
    if slug is None and title.startswith("Review"):
        # Long titles are cut at a word boundary to 32 characters.
        assert slug_name(title) == "review-the-code-changes-in-this"
    else:
        assert slug_name(title) == slug


def test_slug_name_cuts_a_single_long_word() -> None:
    assert slug_name("x" * 50) == "x" * 32


def test_parse_address_with_a_name() -> None:
    a = parse_address("laptop/api@Gateway-Work")
    assert (a.machine, a.repo, a.suffix, a.name) == (
        "laptop",
        "api",
        None,
        "gateway-work",
    )
    assert str(a) == "laptop/api@gateway-work"


@pytest.mark.parametrize("bad", ["laptop/api@", "laptop/api#3f@x", "laptop/api@a@b"])
def test_parse_address_refuses_bad_names(bad: str) -> None:
    with pytest.raises(KampungError) as e:
        parse_address(bad)
    assert e.value.code == "invalid_address"


# -- reported titles


def _name(conn: sqlite3.Connection, s: Session) -> str | None:
    loaded = load_session(conn, s.id)
    assert loaded is not None
    return loaded.name


def _report(
    conn: sqlite3.Connection,
    clock: FakeClock,
    s: Session,
    title: str,
    kind: str = "auto",
) -> str | None:
    with db.transaction(conn):
        return names.report_title(conn, s.id, kind, title, clock.now())


def test_a_reported_title_becomes_the_name_and_list_peers_shows_it(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("desktop")
    assert _report(conn, clock, s, "Gateway work") == "gateway-work"
    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    (peer,) = mail.list_peers(conn, laptop, clock.now())
    assert peer["sessions"][0]["name"] == "gateway-work"  # type: ignore[index]


def test_a_title_with_a_secret_is_dropped(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    s = make_session("desktop")
    _report(conn, clock, s, "Gateway work")
    key = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    assert _report(conn, clock, s, f"use {key}") is None
    assert _name(conn, s) is None


def test_a_taken_name_gets_a_number_that_stays(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    c = make_session("desktop", agent_session_id="c")
    assert _report(conn, clock, a, "Gateway work") == "gateway-work"
    assert _report(conn, clock, b, "Gateway work") == "gateway-work-2"
    assert _report(conn, clock, c, "Gateway work") == "gateway-work-3"
    sessions.end(conn, a.id, clock.now())
    # The same title again: b keeps its number, so its name does not flip.
    assert _report(conn, clock, b, "Gateway work") == "gateway-work-2"
    d = make_session("desktop", agent_session_id="d")
    assert _report(conn, clock, d, "Gateway work") == "gateway-work"  # a freed it


def test_a_numbered_name_still_fits(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, a, "x" * 40)
    assert _report(conn, clock, b, "x" * 40) == "x" * 30 + "-2"


def test_a_revived_session_whose_name_was_taken_gets_a_number(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("desktop", agent_session_id="a")
    _report(conn, clock, a, "Gateway work")
    sessions.end(conn, a.id, clock.now())
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, b, "Gateway work")
    revived = make_session("desktop", agent_session_id="a")
    assert revived.id == a.id and _name(conn, a) == "gateway-work-2"


def test_names_are_per_mailbox(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("desktop", "api")
    b = make_session("desktop", "blog")
    _report(conn, clock, a, "Gateway work")
    assert _report(conn, clock, b, "Gateway work") == "gateway-work"


# -- addressing by name


def _send(
    conn: sqlite3.Connection,
    sender: Session,
    clock: FakeClock,
    to: str,
    mode: Any = "ask",
    **kw: Any,
) -> mail.SendResult:
    return send(conn, sender, to, "hi", None, clock.now(), mode=mode, **kw)


def _error(fn: Any, *args: Any, **kw: Any) -> KampungError:
    with pytest.raises(KampungError) as e:
        fn(*args, **kw)
    return e.value


def _to_session(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT to_session_id FROM messages ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    return None if row is None else row[0]


def test_send_to_an_exact_name(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, a, "Gateway work")
    _report(conn, clock, b, "Gateway docs")
    _send(conn, me, clock, "desktop/api@gateway-docs")
    assert _to_session(conn) == b.id


def test_send_to_a_unique_prefix_for_ask_tell_and_fyi(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, a, "Gateway work")
    _report(conn, clock, b, "Docs review")
    for mode in ("ask", "tell", "fyi"):
        _send(conn, me, clock, "desktop/api@gate", mode)
        assert _to_session(conn) == a.id
    e = _error(_send, conn, me, clock, "desktop/api@ga")  # under 3 characters
    assert e.code == "unknown_name"


def test_an_ambiguous_prefix_is_refused_with_both(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, a, "Gateway work")
    _report(conn, clock, b, "Gateway docs")
    e = _error(_send, conn, me, clock, "desktop/api@gateway")
    assert e.code == "ambiguous_name"
    assert f"desktop/api#{a.suffix} (gateway-work)" in e.hint
    assert f"desktop/api#{b.suffix} (gateway-docs)" in e.hint


def test_an_exact_name_wins_over_a_longer_prefix_match(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, a, "Gateway")
    _report(conn, clock, b, "Gateway docs")
    _send(conn, me, clock, "desktop/api@gateway")
    assert _to_session(conn) == a.id


def test_unknown_name_lists_current_names(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    a = make_session("desktop")
    _report(conn, clock, a, "Gateway work")
    e = _error(_send, conn, me, clock, "desktop/api@nothing")
    assert e.code == "unknown_name"
    assert f"desktop/api#{a.suffix} (gateway-work)" in e.hint


def test_unknown_name_points_to_a_rename(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    a = make_session("desktop")
    _report(conn, clock, a, "Docs review")
    _report(conn, clock, a, "Gateway work")
    e = _error(_send, conn, me, clock, "desktop/api@docs-review")
    assert e.code == "unknown_name"
    assert f'"docs-review" was renamed to "gateway-work" (desktop/api#{a.suffix})' in (
        e.hint
    )
    assert _to_session(conn) is None  # never redirected


def test_unknown_name_points_to_an_ended_session(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    a = make_session("desktop")
    _report(conn, clock, a, "Gateway work")
    sessions.end(conn, a.id, clock.now())
    clock.advance(hours=2)
    e = _error(_send, conn, me, clock, "desktop/api@gateway-work")
    assert e.code == "unknown_name"
    assert '"gateway-work" ended 2 h 00 min ago; send to desktop/api' in e.hint


def test_former_names_are_forgotten_after_a_week(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    a = make_session("desktop")
    _report(conn, clock, a, "Docs review")
    _report(conn, clock, a, "Gateway work")
    clock.advance(days=8)
    e = _error(_send, conn, me, clock, "desktop/api@docs-review")
    assert "renamed" not in e.hint


def test_only_the_last_five_former_names_are_kept(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("desktop")
    for n in range(8):
        _report(conn, clock, a, f"step {n}")
    kept = conn.execute(
        "SELECT name FROM session_names WHERE session_id = ? ORDER BY retired_at",
        (a.id,),
    ).fetchall()
    assert [r[0] for r in kept] == [f"step-{n}" for n in range(2, 7)]


# -- the Claude app: owner instructions and renames


def _grant(conn: sqlite3.Connection, clock: FakeClock, owner: bool = True) -> None:
    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    with db.transaction(conn):
        oauth_store.replace_grant(conn, laptop.id, clock.now())
        oauth_store.set_owner(conn, laptop.id, owner)


@pytest.fixture
def phone(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> Session:
    _grant(conn, clock)
    return make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app"
    )


def test_instruct_needs_the_exact_name(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    _report(conn, clock, a, "Gateway work")
    e = _error(
        _send, conn, phone, clock, "desktop/api@gate", "instruct", connector=True
    )
    assert e.code == "unknown_name"
    assert "exact name" in e.hint
    _send(conn, phone, clock, "desktop/api@gateway-work", "instruct", connector=True)
    assert _to_session(conn) == a.id


def _rename(
    conn: sqlite3.Connection,
    sender: Session,
    clock: FakeClock,
    to: str,
    name: str,
    connector: bool = True,
) -> dict[str, object]:
    return mail.rename(conn, sender, to, name, clock.now(), connector=connector)


def _messages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM messages").fetchone()[0])


def test_rename_from_the_owner_app(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    _report(conn, clock, a, "Review the code")
    result = _rename(conn, phone, clock, f"desktop/api#{a.suffix}", "Gateway Work")
    assert result == {
        "renamed": f"desktop/api#{a.suffix}",
        "old": "review-the-code",
        "name": "gateway-work",
        "offline": False,
    }
    assert _name(conn, a) == "gateway-work" and _messages(conn) == 0
    # The automatic title never beats it.
    _report(conn, clock, a, "Something else")
    assert _name(conn, a) == "gateway-work"


def test_rename_by_exact_name_and_by_a_lone_session(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    _report(conn, clock, a, "Gateway work")
    _rename(conn, phone, clock, "desktop/api@gateway-work", "gw")
    assert _name(conn, a) == "gw"
    _rename(conn, phone, clock, "desktop/api", "gw-two")  # the only session
    assert _name(conn, a) == "gw-two"
    e = _error(_rename, conn, phone, clock, "desktop/api@gw-t", "x")
    assert e.code == "unknown_name"  # no prefix for a rename


def test_rename_needs_a_pick_when_the_mailbox_has_several(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    e = _error(_rename, conn, phone, clock, "desktop/api", "x")
    assert e.code == "pick_session"
    assert f"desktop/api#{a.suffix}" in e.hint and f"desktop/api#{b.suffix}" in e.hint


def test_rename_to_a_taken_name_is_refused(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, a, "Gateway work")
    e = _error(_rename, conn, phone, clock, f"desktop/api#{b.suffix}", "gateway-work")
    assert e.code == "name_taken" and f"desktop/api#{a.suffix}" in e.hint


def test_rename_an_ended_session_is_refused(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    sessions.end(conn, a.id, clock.now())
    e = _error(_rename, conn, phone, clock, f"desktop/api#{a.suffix}", "x")
    assert e.code == "session_ended"


def test_rename_an_offline_session_applies_at_once(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    clock.advance(hours=1)  # past the live window, not ended
    result = _rename(conn, phone, clock, f"desktop/api#{a.suffix}", "gateway-work")
    assert result["offline"] is True and _name(conn, a) == "gateway-work"


def test_an_empty_rename_clears_it(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    _report(conn, clock, a, "Review the code")
    _rename(conn, phone, clock, f"desktop/api#{a.suffix}", "gateway-work")
    result = _rename(conn, phone, clock, f"desktop/api#{a.suffix}", "  ")
    assert result["name"] == "review-the-code" and _name(conn, a) == "review-the-code"


def test_rename_to_nothing_usable_is_refused(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    e = _error(_rename, conn, phone, clock, f"desktop/api#{a.suffix}", "!!!")
    assert e.code == "invalid_name"


def test_rename_is_owner_only(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("desktop")
    me = make_session("laptop")  # a machine-key session
    e = _error(_rename, conn, me, clock, f"desktop/api#{a.suffix}", "x", False)
    assert e.code == "owner_only"
    _grant(conn, clock, owner=False)
    app = make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app"
    )
    e = _error(_rename, conn, app, clock, f"desktop/api#{a.suffix}", "x")
    assert e.code == "owner_only"
    assert _name(conn, a) is None


def test_a_newer_terminal_rename_replaces_a_phone_rename(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    _report(conn, clock, a, "Old work", "rename")
    _rename(conn, phone, clock, f"desktop/api#{a.suffix}", "gateway-work")
    _report(conn, clock, a, "Old work", "rename")  # the same /rename: phone holds
    assert _name(conn, a) == "gateway-work"
    _report(conn, clock, a, "Auto title", "auto")
    assert _name(conn, a) == "gateway-work"
    _report(conn, clock, a, "Api fix", "rename")  # a newer /rename: it wins
    assert _name(conn, a) == "api-fix"


def test_kampung_name_set_before_a_phone_rename_does_not_undo_it(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    _report(conn, clock, a, "fixed-name", "env")
    _rename(conn, phone, clock, f"desktop/api#{a.suffix}", "gateway-work")
    _report(conn, clock, a, "fixed-name", "env")
    assert _name(conn, a) == "gateway-work"


def test_migrate_13_to_14(tmp_path: object) -> None:
    from importlib import resources
    from pathlib import Path

    assert isinstance(tmp_path, Path)
    old = db.connect(tmp_path / "old.db")
    v1 = resources.files("kampung_server").joinpath("schema.sql").read_text()
    for statement in db._statements(v1):
        old.execute(statement)
    for target in range(2, 14):
        for statement in db._statements(db._script(target)):
            old.execute(statement)
    old.execute("PRAGMA user_version=13")
    db.migrate(old)
    cols = {r[1] for r in old.execute("PRAGMA table_info(sessions)")}
    assert {"title", "title_kind", "owner_name", "owner_title_seen", "name"} <= cols
    assert old.execute("SELECT count(*) FROM session_names").fetchone()[0] == 0


def test_the_name_index_only_counts_open_sessions(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, a, "Gateway work")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE sessions SET name = 'gateway-work' WHERE id = ?", (b.id,))
    sessions.end(conn, a.id, clock.now() + timedelta(seconds=1))
    conn.execute("UPDATE sessions SET name = 'gateway-work' WHERE id = ?", (b.id,))


def test_the_sweep_drops_former_names_after_a_week(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    from kampung_server.sweep import sweep

    a = make_session("desktop")
    _report(conn, clock, a, "Docs review")
    _report(conn, clock, a, "Gateway work")
    sweep(conn, clock.now() + timedelta(days=6))
    assert conn.execute("SELECT count(*) FROM session_names").fetchone()[0] == 1
    sweep(conn, clock.now() + timedelta(days=8))
    assert conn.execute("SELECT count(*) FROM session_names").fetchone()[0] == 0


def test_inbox_sender_must_be_a_mailbox_not_a_name(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    me = make_session("laptop")
    e = _error(mail.inbox, conn, me, 20, clock.now(), sender="desktop/api@gateway")
    assert e.code == "invalid_address"


def test_a_rename_carrying_a_secret_is_refused(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop")
    to = f"desktop/api#{a.suffix}"
    e = _error(_rename, conn, phone, clock, to, "https://u:secret123@example.com")
    assert e.code == "secret_detected"
    assert _name(conn, a) is None


def test_a_rename_to_a_freed_name_takes_it_not_the_number(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    a = make_session("desktop", agent_session_id="a")
    b = make_session("desktop", agent_session_id="b")
    _report(conn, clock, a, "Gateway work")
    assert _report(conn, clock, b, "Gateway work") == "gateway-work-2"
    sessions.end(conn, a.id, clock.now())
    result = _rename(conn, phone, clock, f"desktop/api#{b.suffix}", "gateway-work")
    assert result["name"] == "gateway-work" and _name(conn, b) == "gateway-work"


def test_a_rename_refused_as_not_paired_is_audited(
    conn: sqlite3.Connection,
    admin: Any,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    admin.add_machine("other", "admin:cli")
    e = _error(_rename, conn, phone, clock, "other/api#3f", "x")
    assert e.code == "not_paired"
    rows = conn.execute(
        "SELECT detail FROM audit WHERE action = 'denied' ORDER BY rowid DESC LIMIT 1"
    ).fetchall()
    assert rows and '"not_paired"' in rows[0][0]
