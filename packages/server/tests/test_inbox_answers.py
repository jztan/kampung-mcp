"""inbox(answers=true): a Claude app chat that lost the ids it sent lists the
waiting answers by the message they answer, reads nothing, and then claims
its own with replies_to."""

import sqlite3

import pytest

from server_testlib import FakeClock, MakeSession
from kampung_server import db, oauth_store
from kampung_server.admin import AdminService
from kampung_server.attachments import TEXT, Attachment
from kampung_server.errors import KampungError
from kampung_server.limits import MESSAGE_TTL
from kampung_server.mail import ANSWER_PREVIEW, inbox, send
from kampung_server.models import Session, load_machine
from kampung_server.sessions import end


class _AnyStr:
    def __eq__(self, other: object) -> bool:
        return isinstance(other, str)


ANY_STR = _AnyStr()


def _app(make_session: MakeSession, **kw: object) -> Session:
    return make_session(
        "laptop", repo="claude", agent="claude-app", hooks=False, cwd="Claude app", **kw
    )


@pytest.fixture
def app_and_peer(make_session: MakeSession) -> tuple[Session, Session]:
    return _app(make_session), make_session("desktop", "api")


def _ask(
    conn: sqlite3.Connection, app: Session, clock: FakeClock, body: str = "q"
) -> str:
    clock.advance(seconds=1)
    return send(conn, app, "desktop/api", body, None, clock.now()).id


def _answer(
    conn: sqlite3.Connection,
    peer: Session,
    asked: str,
    clock: FakeClock,
    to: str = "laptop/claude",
) -> str:
    return send(conn, peer, to, "a", asked, clock.now()).id


def _undelivered(conn: sqlite3.Connection) -> int:
    sql = "SELECT count(*) FROM messages WHERE delivered_at IS NULL"
    return int(conn.execute(sql).fetchone()[0])


def _inbox_audits(conn: sqlite3.Connection) -> int:
    sql = "SELECT count(*) FROM audit WHERE action = 'inbox'"
    return int(conn.execute(sql).fetchone()[0])


def _code(fn: object, *args: object, **kw: object) -> str:
    with pytest.raises(KampungError) as e:
        fn(*args, **kw)  # type: ignore[operator]
    return e.value.code


@pytest.mark.parametrize("limit", [5, 20])
def test_lists_groups_and_reads_nothing(
    conn: sqlite3.Connection,
    app_and_peer: tuple[Session, Session],
    clock: FakeClock,
    limit: int,
) -> None:
    app, peer = app_and_peer
    assert ANSWER_PREVIEW == 150
    q = _ask(conn, app, clock, "x" * 149 + "é" + "tail")
    _answer(conn, peer, q, clock)
    _answer(conn, peer, q, clock)
    got = inbox(conn, app, limit, clock.now(), answers=True)
    assert got.messages == []
    assert got.answers == [
        {
            "id": q,
            "to": "desktop/api",
            "sent_at": ANY_STR,
            "preview": "x" * 149 + "é" + "…",
            "count": 2,
            "from": ["desktop/api"],
            "owner": False,
        }
    ]
    assert got.answers_more == 0
    assert _undelivered(conn) == 3 and _inbox_audits(conn) == 0
    assert got.to_dict()["answers"] == got.answers


def test_short_preview_is_not_cut(
    conn: sqlite3.Connection,
    app_and_peer: tuple[Session, Session],
    clock: FakeClock,
) -> None:
    app, peer = app_and_peer
    q = _ask(conn, app, clock, "y" * 150)
    _answer(conn, peer, q, clock)
    [g] = inbox(conn, app, 0, clock.now(), answers=True).answers or []
    assert g["preview"] == "y" * 150


def test_excludes_what_replies_to_would_not_read(
    conn: sqlite3.Connection,
    admin: AdminService,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    app, peer = _app(make_session), make_session("desktop", "api")
    blog = make_session("desktop", "blog")
    # an answer to mail another mailbox sent
    theirs = send(conn, blog, "desktop/api", "q", None, clock.now()).id
    _answer(conn, peer, theirs, clock)
    # mail that is not an answer
    send(conn, peer, "laptop/claude", "new", None, clock.now())
    # an answer already read
    read = _ask(conn, app, clock)
    _answer(conn, peer, read, clock)
    inbox(conn, app, 20, clock.now(), replies_to=[read])
    # an answer whose original is deleted from the dashboard
    deleted = _ask(conn, app, clock)
    _answer(conn, peer, deleted, clock)
    admin.delete_message(deleted, "admin:web")
    got = inbox(conn, app, 0, clock.now(), answers=True)
    assert got.answers == [] and got.answers_more == 0


def test_answer_whose_original_expired_is_left_out(
    conn: sqlite3.Connection,
    app_and_peer: tuple[Session, Session],
    clock: FakeClock,
) -> None:
    app, peer = app_and_peer
    q = _ask(conn, app, clock)
    clock.advance(days=MESSAGE_TTL.days - 1)
    _answer(conn, peer, q, clock)
    clock.advance(days=2)  # the original expires, the answer does not
    assert inbox(conn, app, 0, clock.now(), answers=True).answers == []


def test_bound_answer_to_another_live_session_is_left_out(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    app, peer = _app(make_session), make_session("desktop", "api")
    code = make_session("laptop", "claude")
    q = _ask(conn, app, clock)
    _answer(conn, peer, q, clock, to=f"laptop/claude#{code.suffix}")
    assert inbox(conn, app, 0, clock.now(), answers=True).answers == []


def test_original_from_an_ended_session_is_listed(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    old, peer = _app(make_session), make_session("desktop", "api")
    q = _ask(conn, old, clock)
    end(conn, old.id, clock.now())
    _answer(conn, peer, q, clock)
    app = _app(make_session)
    got = inbox(conn, app, 0, clock.now(), answers=True)
    assert [g["id"] for g in got.answers or []] == [q]


def test_owner_instruction_original_is_marked(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    with db.transaction(conn):
        oauth_store.replace_grant(conn, laptop.id, clock.now())
        oauth_store.set_owner(conn, laptop.id, True)
    app, peer = _app(make_session), make_session("desktop", "api")
    q = send(
        conn,
        app,
        "desktop/api",
        "merge it",
        None,
        clock.now(),
        mode="instruct",
        connector=True,
    ).id
    _answer(conn, peer, q, clock)
    [g] = inbox(conn, app, 0, clock.now(), answers=True).answers or []
    assert g["owner"] is True and g["preview"] == "merge it"


def test_attachment_never_in_preview(
    conn: sqlite3.Connection,
    app_and_peer: tuple[Session, Session],
    clock: FakeClock,
) -> None:
    app, peer = app_and_peer
    q = send(
        conn,
        app,
        "desktop/api",
        "see file",
        None,
        clock.now(),
        Attachment("secret.md", TEXT, b"hunter2"),
    ).id
    _answer(conn, peer, q, clock)
    got = inbox(conn, app, 0, clock.now(), answers=True)
    [g] = got.answers or []
    assert g["preview"] == "see file"
    text = repr(got.to_dict())
    assert "secret.md" not in text and "hunter2" not in text


def test_newest_first_and_paging(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    app, peer = _app(make_session), make_session("desktop", "api")
    asked = [_ask(conn, app, clock, f"q{i}") for i in range(21)]
    for q in asked:
        _answer(conn, peer, q, clock)
    first = inbox(conn, app, 0, clock.now(), answers=True)
    ids = [g["id"] for g in first.answers or []]
    assert ids == list(reversed(asked))[:20] and first.answers_more == 1
    older = inbox(conn, app, 0, clock.now(), answers=True, before=ids[-1])
    assert [g["id"] for g in older.answers or []] == [asked[0]]
    assert older.answers_more == 0
    unknown = inbox(conn, app, 0, clock.now(), answers=True, before="msg_" + "0" * 24)
    assert unknown.answers == [] and unknown.answers_more == 0
    foreign = send(conn, peer, "laptop/claude", "x", None, clock.now()).id
    other = inbox(conn, app, 0, clock.now(), answers=True, before=foreign)
    assert other.answers == [] and other.answers_more == 0


def test_round_trip_claims_exactly_the_listed_answers(
    conn: sqlite3.Connection,
    app_and_peer: tuple[Session, Session],
    clock: FakeClock,
) -> None:
    app, peer = app_and_peer
    for n in (1, 2, 25):
        q = _ask(conn, app, clock)
        for _ in range(n):
            _answer(conn, peer, q, clock)
    groups = inbox(conn, app, 0, clock.now(), answers=True).answers or []
    assert sorted(g["count"] for g in groups) == [1, 2, 25]
    for g in groups:
        read: list[dict[str, object]] = []
        while True:
            got = inbox(conn, app, 20, clock.now(), replies_to=[str(g["id"])])
            read += got.messages
            if not got.more:
                break
        assert len(read) == g["count"]
        assert {m["reply_to"] for m in read} == {g["id"]}
    assert inbox(conn, app, 0, clock.now(), answers=True).answers == []


def test_background_session_listing_matches_its_claim(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    make_session("laptop", "claude")  # a live foreground session there
    app = _app(make_session, background=True)
    peer = make_session("desktop", "api")
    q = _ask(conn, app, clock)
    _answer(conn, peer, q, clock)
    groups = inbox(conn, app, 0, clock.now(), answers=True).answers or []
    claimed = inbox(conn, app, 20, clock.now(), replies_to=[q]).messages
    assert sum(int(str(g["count"])) for g in groups) == len(claimed)


def test_refusals(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    app, code = _app(make_session), make_session("laptop", "api")
    make_session("desktop", "api")
    q = _ask(conn, app, clock)
    now = clock.now()
    assert _code(inbox, conn, code, 0, now, answers=True) == "invalid_answers"
    assert (
        _code(inbox, conn, app, 0, now, answers=True, sender="desktop/api")
        == "invalid_answers"
    )
    assert (
        _code(inbox, conn, app, 0, now, answers=True, replies_to=[q])
        == "invalid_answers"
    )
    assert _code(inbox, conn, app, 0, now, before=q) == "invalid_answers"
    assert (
        _code(inbox, conn, app, 0, now, answers=True, before="nope")
        == "invalid_answers"
    )


def test_old_shape_without_answers(
    conn: sqlite3.Connection,
    app_and_peer: tuple[Session, Session],
    clock: FakeClock,
) -> None:
    app, _ = app_and_peer
    result = inbox(conn, app, 20, clock.now()).to_dict()
    assert "answers" not in result and "answers_more" not in result
