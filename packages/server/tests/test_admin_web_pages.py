import sqlite3
from dataclasses import dataclass

import pytest
from starlette.testclient import TestClient

from admin_web_testlib import login
from kampung_server import mail
from kampung_server.admin import AdminService
from server_testlib import FakeClock, MakeSession

PAGES = [
    "/admin",
    "/admin/machines",
    "/admin/mailboxes",
    "/admin/sessions",
    "/admin/messages",
    "/admin/conversations",
    "/admin/audit",
]


@dataclass
class Seeded:
    message_id: str
    sender: str


@pytest.fixture
def seeded(
    conn: sqlite3.Connection, make_session: MakeSession, clock: FakeClock
) -> Seeded:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    sent = mail.send(
        conn, a, "desktop/api", "please check the build", None, clock.now()
    )
    return Seeded(sent.id, a.address)


def test_overview_shows_counts_and_activity(web: TestClient, seeded: Seeded) -> None:
    login(web)
    page = web.get("/admin").text
    for label in ("Machines", "Live sessions", "Queued", "No-wake", "Last 24 h"):
        assert label in page
    assert "admin:cli" in page  # recent activity


def test_add_machine_help_matches_validate_name(
    web: TestClient, seeded: Seeded
) -> None:
    login(web)
    page = web.get("/admin/machines").text
    assert "Letters, digits, dots, underscores and dashes" in page
    assert "up to 64 characters" in page
    assert "starting with a letter or digit" in page


def test_machines_page(web: TestClient, seeded: Seeded) -> None:
    login(web)
    page = web.get("/admin/machines").text
    assert "laptop" in page and "desktop" in page and "Active" in page
    assert 'action="/admin/machines"' in page and 'action="/admin/pairs"' in page


def test_mailboxes_page(web: TestClient, seeded: Seeded) -> None:
    login(web)
    page = web.get("/admin/mailboxes").text
    assert "laptop/api" in page and "desktop/api" in page


def test_sessions_page_states_and_filter(
    web: TestClient, seeded: Seeded, admin: AdminService, conn: sqlite3.Connection
) -> None:
    login(web)
    page = web.get("/admin/sessions").text
    assert seeded.sender in page and "Live" in page
    sid = conn.execute("SELECT id FROM sessions ORDER BY id LIMIT 1").fetchone()[0]
    admin.end_session(sid, "admin:cli")
    ended = web.get("/admin/sessions?state=ended").text
    assert seeded.sender in ended and "Ended" in ended
    assert seeded.sender not in web.get("/admin/sessions").text


def test_messages_page_and_detail(web: TestClient, seeded: Seeded) -> None:
    login(web)
    page = web.get("/admin/messages").text
    assert f'href="/admin/messages/{seeded.message_id}"' in page
    detail = web.get(f"/admin/messages/{seeded.message_id}").text
    for text in (seeded.sender, "desktop/api", "not an instruction"):
        assert text in detail
    assert "please check the build" in detail


def test_message_body_is_escaped(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    sent = mail.send(
        conn, a, "desktop/api", "<script>alert(1)</script>", None, clock.now()
    )
    login(web)
    page = web.get(f"/admin/messages/{sent.id}").text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "<script>alert" not in page


def test_unknown_message_is_404(web: TestClient) -> None:
    login(web)
    r = web.get("/admin/messages/nope")
    assert r.status_code == 404 and "No such message" in r.text


def test_audit_filters(web: TestClient, seeded: Seeded, admin: AdminService) -> None:
    admin.unpair("laptop", "desktop", "admin:web")
    login(web)
    page = web.get("/admin/audit?actor=admin:web").text
    assert "<td>unpair</td>" in page and "<td>add_machine</td>" not in page


def test_every_page_needs_login(web: TestClient) -> None:
    for path in PAGES + ["/admin/messages/x"]:
        r = web.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/login", path


def test_pages_have_no_inline_styles(web: TestClient, seeded: Seeded) -> None:
    login(web)
    for path in PAGES + [f"/admin/messages/{seeded.message_id}", "/admin/login"]:
        html = web.get(path).text
        assert ' style="' not in html and "<style" not in html, path


def test_refresh_keeps_filters_and_avoids_post_urls(
    web: TestClient, seeded: Seeded
) -> None:
    csrf = login(web)
    page = web.get("/admin/messages?state=all&no_wake=1").text
    assert 'href="/admin/messages?state=all&amp;no_wake=1">Refresh' in page
    expired = web.post("/admin/sessions/1/end", data={"csrf": "stale"})
    assert expired.status_code == 403
    assert 'href="/admin/sessions">Refresh' in expired.text
    assert csrf


def test_refresh_never_points_at_a_post_only_path(web: TestClient) -> None:
    login(web)
    for path, section in (("/admin/logout", "/admin"), ("/admin/pairs", "/admin")):
        page = web.post(path, data={"csrf": "stale"})
        assert page.status_code == 403
        assert f'href="{section}">Refresh' in page.text, path


AUTO = "data-auto-refresh"


def test_list_pages_auto_refresh(web: TestClient, seeded: Seeded) -> None:
    login(web)
    for path in PAGES + [f"/admin/messages/{seeded.message_id}"]:
        page = web.get(path).text
        assert AUTO in page, path
        assert '<script src="/admin/static/admin.js" defer></script>' in page, path


def test_login_error_and_confirm_pages_do_not_auto_refresh(
    web: TestClient, seeded: Seeded
) -> None:
    assert AUTO not in web.get("/admin/login").text
    login(web)
    for path in (
        "/admin/messages/msg_missing",
        "/admin/machines/laptop/revoke",
        f"/admin/messages/{seeded.message_id}/delete",
    ):
        page = web.get(path).text
        assert AUTO not in page and "admin.js" not in page, path


def test_pages_have_no_inline_scripts(web: TestClient, seeded: Seeded) -> None:
    login(web)
    for path in PAGES:
        page = web.get(path).text
        assert page.count("<script") == page.count("<script src="), path


def test_audit_shows_notable_events_by_default(
    web: TestClient, seeded: Seeded, admin: AdminService
) -> None:
    admin.unpair("laptop", "desktop", "admin:web")
    login(web)
    notable = web.get("/admin/audit").text
    assert "<td>unpair</td>" in notable and "<td>send</td>" not in notable
    every = web.get("/admin/audit?show=all").text
    assert "<td>send</td>" in every
    picked = web.get("/admin/audit?action=send").text
    assert "<td>send</td>" in picked and "<td>unpair</td>" not in picked


def test_audit_detail_links_messages(web: TestClient, seeded: Seeded) -> None:
    login(web)
    page = web.get("/admin/audit?show=all").text
    assert f'href="/admin/messages/{seeded.message_id}"' in page
    assert '{"bytes"' not in page


def test_conversations_list_and_thread(
    web: TestClient,
    seeded: Seeded,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    reply = make_session("desktop", "api")
    mail.send(conn, reply, "laptop/api", "<b>done</b>", None, clock.now())
    login(web)
    listing = web.get("/admin/conversations").text
    assert 'href="/admin/conversations?a=desktop/api&amp;b=laptop/api"' in listing
    thread = web.get("/admin/conversations?a=desktop/api&b=laptop/api").text
    assert "please check the build" in thread
    assert "&lt;b&gt;done&lt;/b&gt;" in thread and "<b>done</b>" not in thread
    unknown = web.get("/admin/conversations?a=nope/x&b=laptop/api").text
    assert "please check the build" not in unknown


def test_messages_page_shows_attachment_metadata_only(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    from kampung_server.attachments import parse_attachment

    a = make_session("laptop", "api")
    make_session("desktop", "api")
    att = parse_attachment(
        {"name": "notes_v2.md", "text": "attachment-content-here"}, connector=False
    )
    sent = mail.send(conn, a, "desktop/api", "see notes", None, clock.now(), att)
    login(web)
    page = web.get("/admin/messages").text
    listed = page.split("<tbody>", 1)[1]
    assert "notes_v2.md" in listed and "23 B" in listed
    assert "attachment-content-here" not in page
    detail = web.get(f"/admin/messages/{sent.id}").text
    assert "notes_v2.md" in detail and "23 B" in detail and "text" in detail
    assert "attachment-content-here" not in detail


def test_sessions_page_shows_wakes_used_and_refill_button(
    web: TestClient, conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    s = make_session("laptop", "api")
    conn.execute(
        "UPDATE mailboxes SET wakes_since_refill = 17 WHERE id = ?", (s.mailbox_id,)
    )
    login(web)
    page = web.get("/admin/sessions").text
    assert "Wakes used" in page and ">17<" in page
    assert f"/admin/sessions/{s.id}/refill" in page and "Depth" not in page


def test_message_pages_show_wake_reason(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    held = mail.send(conn, a, "desktop/api", "later", None, clock.now(), mode="fyi")
    old = mail.send(conn, a, "desktop/api", "old", None, clock.now())
    conn.execute(
        "UPDATE messages SET depth = 4, wake = 0, wake_reason = NULL WHERE id = ?",
        (old.id,),
    )
    login(web)
    listing = web.get("/admin/messages").text
    assert "held: fyi" in listing and "held: depth 4 (before v5)" in listing
    assert "No-wake only" in listing and "over 3" not in listing
    detail = web.get(f"/admin/messages/{held.id}").text
    assert "held: fyi" in detail and "depth over 3" not in detail
    thread = web.get("/admin/conversations?a=desktop/api&b=laptop/api").text
    assert "held: fyi" in thread


def test_message_pages_show_the_send_mode(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    tell = mail.send(conn, a, "desktop/api", "act", None, clock.now(), mode="tell")
    mail.send(conn, a, "desktop/api", "q", None, clock.now())
    login(web)
    listing = web.get("/admin/messages").text
    assert "<th>Mode</th>" in listing
    assert ">tell<" in listing and ">ask<" in listing
    detail = web.get(f"/admin/messages/{tell.id}").text
    assert "<dt>Mode</dt>" in detail and ">tell<" in detail
    assert "no reply needed" in detail
    thread = web.get("/admin/conversations?a=desktop/api&b=laptop/api").text
    assert ">tell<" in thread


def test_sessions_page_tags_background_sessions(
    web: TestClient, make_session: MakeSession
) -> None:
    make_session("laptop", "api")
    login(web)
    assert ">background<" not in web.get("/admin/sessions").text
    make_session("laptop", "api", background=True)
    assert ">background<" in web.get("/admin/sessions").text


def test_a_send_to_background_sessions_only_is_labelled(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api", background=True)
    r = mail.send(conn, a, "desktop/api", "late", None, clock.now())
    login(web)
    assert "wakes (background only)" in web.get(f"/admin/messages/{r.id}").text


def test_machines_page_owner_column(
    web: TestClient,
    conn: sqlite3.Connection,
    seeded: Seeded,
    admin: AdminService,
    clock: FakeClock,
) -> None:
    from kampung_server import db, oauth_store
    from kampung_server.models import load_machine

    login(web)
    page = web.get("/admin/machines").text
    assert "/owner" not in page  # no grant, no buttons
    laptop = load_machine(conn, "laptop")
    assert laptop is not None
    with db.transaction(conn):
        oauth_store.replace_grant(conn, laptop.id, clock.now())
    page = web.get("/admin/machines").text
    assert 'action="/admin/machines/laptop/owner"' in page
    assert 'action="/admin/machines/desktop/owner"' not in page
    admin.owner_add("laptop", "admin:cli")
    page = web.get("/admin/machines").text
    assert 'action="/admin/machines/laptop/owner/delete"' in page
    assert "Owner" in page


def test_messages_page_marks_owner(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    from test_admin_web_queries import _owner_instruction

    msg_id = _owner_instruction(conn, make_session, clock)
    login(web)
    assert ">instruct<" in web.get("/admin/messages").text
    assert "instruct" in web.get(f"/admin/messages/{msg_id}").text
