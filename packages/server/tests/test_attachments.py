import base64
import io
import sqlite3
import threading
from pathlib import Path

import pytest
from PIL import Image

from kampung_server import attachments, db
from kampung_server.attachments import Attachment, parse_attachment, valid_name
from kampung_server.errors import KampungError
from kampung_server.mail import inbox, send
from server_testlib import FakeClock, MakeSession

KEY = "ghp_" + "a" * 36


def _png_b64(w: int = 30, h: int = 20) -> str:
    out = io.BytesIO()
    Image.new("RGB", (w, h), (10, 120, 200)).save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def _text(name: str, text: str) -> Attachment:
    return parse_attachment({"name": name, "text": text}, connector=False)


def _row(conn: sqlite3.Connection, message_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT name, type, bytes, data FROM attachments WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    assert row is not None
    return row


def _err(fn: object, *a: object, **kw: object) -> KampungError:
    with pytest.raises(KampungError) as e:
        fn(*a, **kw)  # type: ignore[operator]
    return e.value


# Names: the same vectors the client tests use.
GOOD_NAMES = ["README.md", "a", "shot-1.png", "notes_v2.txt", "x" * 100]
BAD_NAMES = [
    "../x",
    "a/b",
    "a\\b",
    "D:x",
    "a.txt:stream",
    "CON",
    "con.txt",
    "Com1.log",
    "CONIN$",
    "a.",
    ".hidden",
    "x" * 101,
    "",
    "a b",
    "na‮me",
    "line\nbreak",
]


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_good_names(name: str) -> None:
    assert valid_name(name)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_bad_names(name: str) -> None:
    assert not valid_name(name)


def test_text_attachment_is_stored_beside_the_message(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, b = make_session("laptop"), make_session("desktop", "blog")
    sent = send(
        conn,
        a,
        "desktop/blog",
        "see file",
        None,
        clock.now(),
        attachment=_text("README.md", "# Hi\n"),
    )
    row = _row(conn, sent.id)
    assert (row["name"], row["type"], row["bytes"]) == ("README.md", "text", 5)
    assert bytes(row["data"]) == b"# Hi\n"
    assert inbox(conn, b, 20, clock.now()).messages[0]["body"] == "see file"


def test_image_attachment_is_stored_re_encoded(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, _ = make_session("laptop"), make_session("desktop", "blog")
    raw = _png_b64()
    att = parse_attachment(
        {"name": "shot.png", "image": raw, "type": "image/png"}, connector=False
    )
    sent = send(conn, a, "desktop/blog", "look", None, clock.now(), attachment=att)
    row = _row(conn, sent.id)
    assert row["type"] in ("image/png", "image/jpeg")
    assert bytes(row["data"]) != base64.b64decode(raw)
    assert Image.open(io.BytesIO(bytes(row["data"]))).size == (30, 20)


def test_connector_cannot_send_an_image() -> None:
    e = _err(
        parse_attachment,
        {"name": "s.png", "image": _png_b64(), "type": "image/png"},
        connector=True,
    )
    assert e.code == "invalid_attachment"
    assert (
        parse_attachment({"name": "spec.md", "text": "x"}, connector=True).type
        == "text"
    )


@pytest.mark.parametrize(
    "raw",
    [
        "not a dict",
        {},
        {"name": "a.md"},
        {"name": "a.md", "text": "x", "image": "eA=="},
        {"name": "a.md", "text": "x", "extra": "y"},
        {"name": "../a.md", "text": "x"},
        {"name": "a.md", "text": 5},
        {"name": "a.png", "image": "not base64!!", "type": "image/png"},
        {"name": "a.png", "image": _png_b64(), "type": "image/gif"},
        {"name": "a.png", "image": _png_b64()},
    ],
)
def test_bad_shapes_are_invalid(raw: object) -> None:
    assert _err(parse_attachment, raw, connector=False).code == "invalid_attachment"


def test_empty_text_is_invalid() -> None:
    assert _err(_text, "a.md", "").code == "invalid_attachment"


def test_text_over_256_kb_is_too_large() -> None:
    assert _err(_text, "a.md", "x" * (256 * 1024 + 1)).code == "attachment_too_large"
    assert _text("a.md", "x" * (256 * 1024)).type == "text"


def test_secret_in_the_body_rejects_the_send(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, _ = make_session("laptop"), make_session("desktop", "blog")
    e = _err(send, conn, a, "desktop/blog", f"one\ntwo\n{KEY}", None, clock.now())
    assert e.code == "secret_detected"
    assert "GitHub token" in e.hint and "line 3" in e.hint and KEY not in e.hint
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_secret_in_the_attachment_names_its_line_and_stores_nothing(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, _ = make_session("laptop"), make_session("desktop", "blog")
    e = _err(
        send,
        conn,
        a,
        "desktop/blog",
        "body",
        None,
        clock.now(),
        attachment=_text("env.txt", f"A=1\n{KEY}\n"),
    )
    assert e.code == "secret_detected"
    assert "line 2 of the attachment" in e.hint and KEY not in e.hint
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    audit = " ".join(r[0] for r in conn.execute("SELECT detail FROM audit"))
    assert KEY not in audit and "a" * 36 not in audit


def test_key_split_between_body_and_attachment_is_caught(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, _ = make_session("laptop"), make_session("desktop", "blog")
    e = _err(
        send,
        conn,
        a,
        "desktop/blog",
        "token follows: ghp_",
        None,
        clock.now(),
        attachment=_text("rest.txt", "a" * 36),
    )
    assert e.code == "secret_detected"


def test_unread_cap_per_sending_machine(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments, "MAX_UNREAD_ATTACHMENT_BYTES_PER_MACHINE", 10)
    a, b = make_session("laptop"), make_session("desktop", "blog")
    send(
        conn,
        a,
        "desktop/blog",
        "1",
        None,
        clock.now(),
        attachment=_text("a.md", "x" * 8),
    )
    e = _err(
        send,
        conn,
        a,
        "desktop/blog",
        "2",
        None,
        clock.now(),
        attachment=_text("b.md", "x" * 8),
    )
    assert e.code == "attachments_full"
    inbox(conn, b, 20, clock.now())
    send(
        conn,
        a,
        "desktop/blog",
        "3",
        None,
        clock.now(),
        attachment=_text("c.md", "x" * 8),
    )


def test_server_wide_cap(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES_TOTAL", 10)
    a, b = make_session("laptop"), make_session("desktop", "blog")
    send(
        conn,
        a,
        "desktop/blog",
        "1",
        None,
        clock.now(),
        attachment=_text("a.md", "x" * 8),
    )
    inbox(conn, b, 20, clock.now())  # read, but still stored until it expires
    e = _err(
        send,
        conn,
        a,
        "desktop/blog",
        "2",
        None,
        clock.now(),
        attachment=_text("b.md", "x" * 8),
    )
    assert e.code == "attachments_full"


def test_low_disk_refuses_attachments_but_not_plain_mail(
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments, "free_disk_bytes", lambda conn: 10)
    a, _ = make_session("laptop"), make_session("desktop", "blog")
    e = _err(
        send,
        conn,
        a,
        "desktop/blog",
        "1",
        None,
        clock.now(),
        attachment=_text("a.md", "x"),
    )
    assert e.code == "attachments_full"
    send(conn, a, "desktop/blog", "plain", None, clock.now())


def test_two_racing_sends_cannot_both_take_the_last_bytes(
    db_path: Path,
    make_session: MakeSession,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attachments, "MAX_UNREAD_ATTACHMENT_BYTES_PER_MACHINE", 10)
    a, _ = make_session("laptop"), make_session("desktop", "blog")
    results: list[str] = []
    gate = threading.Barrier(2)

    def go(n: int) -> None:
        c = db.connect(db_path)
        try:
            gate.wait()
            send(
                c,
                a,
                "desktop/blog",
                str(n),
                None,
                clock.now(),
                attachment=_text(f"{n}.md", "x" * 8),
            )
            results.append("ok")
        except KampungError as e:
            results.append(e.code)
        finally:
            c.close()

    threads = [threading.Thread(target=go, args=(n,)) for n in (1, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["attachments_full", "ok"]


def test_audit_records_name_type_and_bytes(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, _ = make_session("laptop"), make_session("desktop", "blog")
    send(
        conn,
        a,
        "desktop/blog",
        "b",
        None,
        clock.now(),
        attachment=_text("a.md", "hello"),
    )
    detail = conn.execute("SELECT detail FROM audit WHERE action = 'send'").fetchone()[
        0
    ]
    assert '"name": "a.md"' in detail and '"type": "text"' in detail
    assert "hello" not in detail


def test_send_without_attachment_is_unchanged(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> None:
    a, _ = make_session("laptop"), make_session("desktop", "blog")
    sent = send(conn, a, "desktop/blog", "plain", None, clock.now())
    assert set(sent.to_dict()) == {"id", "depth", "wake"}
    assert conn.execute("SELECT count(*) FROM attachments").fetchone()[0] == 0
