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
