"""Owner instructions: send with mode "instruct" from the owner's Claude app.
Only an owner grant may send one; it is bound to one live session when sent."""

import json
import sqlite3
from typing import Any

import pytest

from kampung_server import db, mail, oauth_store
from kampung_server.admin import AdminService
from kampung_server.clock import ts
from kampung_server.errors import KampungError
from kampung_server.guard import GuardSettings
from kampung_server.mail import send
from kampung_server.models import Session, load_machine
from server_testlib import FakeClock, MakeSession

G = GuardSettings()
NOT_OWNER_HINT = (
    "Only the owner's Claude app may send owner instructions. "
    "Send with mode ask or tell."
)


def _grant(conn: sqlite3.Connection, clock: FakeClock, owner: bool = True) -> None:
    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    with db.transaction(conn):
        oauth_store.replace_grant(conn, laptop.id, clock.now())
        oauth_store.set_owner(conn, laptop.id, owner)


def _phone(make_session: MakeSession) -> Session:
    return make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app"
    )


@pytest.fixture
def phone(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> Session:
    _grant(conn, clock)
    return _phone(make_session)


def _instruct(
    conn: sqlite3.Connection,
    sender: Session,
    clock: FakeClock,
    to: str = "desktop/api",
    body: str = "merge it",
    connector: bool = True,
    **kw: Any,
) -> mail.SendResult:
    return send(
        conn,
        sender,
        to,
        body,
        None,
        clock.now(),
        mode="instruct",
        connector=connector,
        **kw,
    )


def _error(fn: Any, *args: Any, **kw: Any) -> KampungError:
    with pytest.raises(KampungError) as e:
        fn(*args, **kw)
    return e.value


def _denied(conn: sqlite3.Connection) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT detail FROM audit WHERE action = 'denied' ORDER BY id"
    ).fetchall()
    return [json.loads(r[0]) for r in rows]


def _messages(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT count(*) FROM messages").fetchone()[0])


# The owner check, first of all


def test_instruct_from_owner_is_stored_bound(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    api = make_session("desktop", "api")
    r = _instruct(conn, phone, clock)
    assert r.wake is True
    assert r.to == f"desktop/api#{api.suffix}"
    row = conn.execute(
        "SELECT owner, to_session_id, fyi, no_reply FROM messages"
    ).fetchone()
    assert tuple(row) == (1, api.id, 0, 0)
    detail = json.loads(
        conn.execute(
            "SELECT detail FROM audit WHERE action = 'send' ORDER BY id DESC"
        ).fetchone()[0]
    )
    assert detail["owner"] is True and detail["mode"] == "instruct"


def test_instruct_from_non_owner_grant(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    _grant(conn, clock, owner=False)
    p = _phone(make_session)
    make_session("desktop", "api")
    e = _error(_instruct, conn, p, clock)
    assert (e.code, e.hint) == ("not_owner", NOT_OWNER_HINT)
    assert _messages(conn) == 0
    assert _denied(conn) == [{"to": "desktop/api", "reason": "not_owner"}]


def test_instruct_from_machine_key(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    laptop_api = make_session("laptop", "api")
    make_session("desktop", "api")
    assert _error(_instruct, conn, laptop_api, clock, connector=False).code == (
        "not_owner"
    )
    # A machine-key session on the owner's machine with the flag set: still no.
    assert _error(_instruct, conn, laptop_api, clock, connector=True).code == (
        "not_owner"
    )
    assert _messages(conn) == 0
    assert len(_denied(conn)) == 2


@pytest.mark.parametrize("case", ["unknown", "unpaired", "full"])
def test_not_owner_comes_before_lookups(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    admin: AdminService,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    _grant(conn, clock, owner=False)
    p = _phone(make_session)
    make_session("desktop", "api")
    to = "desktop/api"
    if case == "unknown":
        to = "nosuch/api"
    elif case == "unpaired":
        admin.add_machine("far", "admin:cli")
        to = "far/api"
    else:
        monkeypatch.setattr(mail, "MAX_WAITING_PER_MAILBOX", 0)
    assert _error(_instruct, conn, p, clock, to=to).code == "not_owner"
    assert _denied(conn) == [{"to": to, "reason": "not_owner"}]


def test_owner_remove_bites_next_send(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    make_session("desktop", "api")
    _instruct(conn, phone, clock)
    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    with db.transaction(conn):
        oauth_store.set_owner(conn, laptop.id, False)
    assert _error(_instruct, conn, phone, clock).code == "not_owner"


def test_peer_modes_need_no_owner(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    api = make_session("desktop", "api")
    other = make_session("laptop", "web")
    r = send(conn, other, "desktop/api", "hi", None, clock.now(), mode="tell")
    assert r.to is None
    assert conn.execute("SELECT owner, no_reply FROM messages").fetchone()[:] == (0, 1)
    assert api.id  # the receiver exists; nothing bound


# Refusals after the owner check


def test_instruct_with_attachment(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    from kampung_server.attachments import parse_attachment

    make_session("desktop", "api")
    att = parse_attachment({"name": "a.md", "text": "x"}, connector=True)
    e = _error(_instruct, conn, phone, clock, attachment=att)
    assert e.code == "attachment_not_allowed"
    assert e.hint == (
        "An owner instruction is text only. Drop the attachment, or send it "
        "with mode tell."
    )


@pytest.mark.parametrize("suffix", [False, True])
def test_instruct_to_the_app(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
    suffix: bool,
) -> None:
    to = f"laptop/claude#{phone.suffix}" if suffix else "laptop/claude"
    e = _error(_instruct, conn, phone, clock, to=to)
    assert e.code == "cannot_instruct"
    assert e.hint == (
        "laptop/claude is the Claude app. Owner instructions go to agent sessions."
    )


# Binding to one session


def test_binds_the_one_bindable_session(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    api = make_session("desktop", "api")
    assert _instruct(conn, phone, clock).to == f"desktop/api#{api.suffix}"


def test_prefers_the_single_foreground(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    make_session("desktop", "api", background=True, agent_session_id="bg")
    fg = make_session("desktop", "api", agent_session_id="fg")
    assert _instruct(conn, phone, clock).to == f"desktop/api#{fg.suffix}"


@pytest.mark.parametrize("background", [False, True])
def test_ambiguous_when_several(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
    background: bool,
) -> None:
    a = make_session("desktop", "api", background=background, agent_session_id="a")
    b = make_session("desktop", "api", background=background, agent_session_id="b")
    e = _error(_instruct, conn, phone, clock)
    assert e.code == "ambiguous_session"
    assert e.hint == (
        "Several sessions are working in desktop/api: "
        f"desktop/api#{a.suffix}, desktop/api#{b.suffix}. Say which one."
    )
    assert _messages(conn) == 0


def test_no_session_when_none(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    api = make_session("desktop", "api")
    with db.transaction(conn):
        conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", ("x", api.id))
    e = _error(_instruct, conn, phone, clock)
    assert e.code == "no_session"
    assert e.hint == (
        "Nobody is working in desktop/api now. An owner instruction needs a "
        "live session; use send to leave a note."
    )


def _seen(conn: sqlite3.Connection, s: Session, when: object) -> None:
    conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (when, s.id))


def test_polling_session_stale_after_two_minutes(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    dead = make_session("desktop", "api", idle_wake=True, agent_session_id="dead")
    clock.advance(minutes=3)
    assert _error(_instruct, conn, phone, clock).code == "no_session"
    fresh = make_session("desktop", "api", idle_wake=True, agent_session_id="new")
    assert dead.id != fresh.id
    assert _instruct(conn, phone, clock).to == f"desktop/api#{fresh.suffix}"


def test_non_polling_session_uses_live_window(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    api = make_session("desktop", "api")
    clock.advance(minutes=20)
    assert _instruct(conn, phone, clock).to == f"desktop/api#{api.suffix}"
    clock.advance(minutes=11)
    assert _error(_instruct, conn, phone, clock).code == "no_session"


@pytest.mark.parametrize("typed", [None, 61, 59])
def test_codex_needs_recent_typing(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
    typed: int | None,
) -> None:
    from datetime import timedelta

    codex = make_session("desktop", "api", agent="codex", agent_session_id="t1")
    if typed is not None:
        conn.execute(
            "UPDATE sessions SET typed_at = ? WHERE id = ?",
            (ts(clock.now() - timedelta(minutes=typed)), codex.id),
        )
    if typed == 59:
        assert _instruct(conn, phone, clock).to == f"desktop/api#{codex.suffix}"
    else:
        assert _error(_instruct, conn, phone, clock).code == "no_session"


def test_suffix_must_be_bindable(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    api = make_session("desktop", "api", idle_wake=True)
    to = f"desktop/api#{api.suffix}"
    assert _instruct(conn, phone, clock, to=to).to == to
    clock.advance(minutes=3)
    assert _error(_instruct, conn, phone, clock, to=to).code == "no_session"
    with db.transaction(conn):
        conn.execute("UPDATE sessions SET ended_at = ? WHERE id = ?", ("x", api.id))
    assert _error(_instruct, conn, phone, clock, to=to).code == "unknown_session"


def test_wake_budget_hint_for_instruct(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    make_session("desktop", "api")
    for _ in range(G.connector_wake_budget):
        assert send(conn, phone, "desktop/api", "x", None, clock.now()).wake
    r = _instruct(conn, phone, clock)
    assert r.wake is False and r.reason == "wake_budget"
    assert r.hint is not None and r.hint.endswith(
        "It is held, and expires unread after 60 minutes unless that session "
        "reads its inbox."
    )
    assert "next inbox" not in r.hint


def test_background_hint_for_instruct(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    phone: Session,
) -> None:
    make_session("desktop", "api", background=True)
    r = _instruct(conn, phone, clock)
    assert r.note == "background"
    assert r.hint is not None and "expires unread after 60 minutes" in r.hint
    assert "Expect a late reply" not in r.hint
