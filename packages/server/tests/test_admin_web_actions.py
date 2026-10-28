import sqlite3

from starlette.testclient import TestClient

from admin_web_testlib import login
from kampung_server import mail
from kampung_server.models import Machine, load_machine, load_session
from server_testlib import FakeClock, MakeSession

Machines = dict[str, tuple[Machine, str]]


def audit_rows(conn: sqlite3.Connection, actor: str = "admin:web") -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT action FROM audit WHERE actor = ? ORDER BY id", (actor,)
        )
    ]


def test_new_key_shown_once_and_never_in_url(
    web: TestClient, conn: sqlite3.Connection
) -> None:
    csrf = login(web)
    r = web.post(
        "/admin/machines", data={"csrf": csrf, "name": "vps"}, follow_redirects=False
    )
    assert r.status_code == 303 and "kmp_" not in r.headers["location"]
    first = web.get(r.headers["location"]).text
    assert first.count("kmp_") == 1
    assert "kmp_" not in web.get("/admin/machines").text
    details = [r[0] for r in conn.execute("SELECT detail FROM audit")]
    assert details and not any("kmp_" in d for d in details)
    assert audit_rows(conn) == ["add_machine"]


def test_create_machine_name_taken(web: TestClient, machines: Machines) -> None:
    csrf = login(web)
    page = web.post("/admin/machines", data={"csrf": csrf, "name": "laptop"}).text
    assert "already exists" in page and "kmp_" not in page


def test_pair_and_unpair(
    web: TestClient, conn: sqlite3.Connection, machines: Machines
) -> None:
    csrf = login(web)
    form = {"csrf": csrf, "a": "laptop", "b": "desktop"}
    assert (
        "Unpaired laptop and desktop" in web.post("/admin/pairs/delete", data=form).text
    )
    assert "Paired laptop and desktop" in web.post("/admin/pairs", data=form).text
    assert audit_rows(conn) == ["unpair", "pair"]


def test_revoke_needs_typed_name(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
) -> None:
    s = make_session("laptop", "api")
    csrf = login(web)
    page = web.get("/admin/machines/laptop/revoke").text
    assert "Type" in page and "laptop" in page
    wrong = web.post(
        "/admin/machines/laptop/revoke", data={"csrf": csrf, "confirm": "lap"}
    )
    assert "did not match" in wrong.text
    machine = load_machine(conn, "laptop")
    assert machine is not None and machine.revoked_at is None
    ok = web.post(
        "/admin/machines/laptop/revoke", data={"csrf": csrf, "confirm": "laptop"}
    )
    assert "Revoked laptop" in ok.text
    machine = load_machine(conn, "laptop")
    assert machine is not None and machine.revoked_at is not None
    ended = load_session(conn, s.id)
    assert ended is not None and ended.ended_at is not None


def test_end_and_reset_depth(
    web: TestClient, conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    s = make_session("laptop", "api")
    conn.execute("UPDATE sessions SET max_depth_seen = 4 WHERE id = ?", (s.id,))
    conn.commit()
    csrf = login(web)
    page = web.post(f"/admin/sessions/{s.id}/reset-depth", data={"csrf": csrf}).text
    assert f"Reset depth for {s.address}" in page
    reset = load_session(conn, s.id)
    assert reset is not None and reset.max_depth_seen == -1
    page = web.post(f"/admin/sessions/{s.id}/end", data={"csrf": csrf}).text
    assert f"Ended {s.address}" in page
    ended = load_session(conn, s.id)
    assert ended is not None and ended.ended_at is not None
    assert audit_rows(conn) == ["reset_depth", "end_session"]


def test_delete_message_confirm_flow(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    sent = mail.send(conn, a, "desktop/api", "oops", None, clock.now())
    csrf = login(web)
    confirm = web.get(f"/admin/messages/{sent.id}/delete").text
    assert "Delete this message?" in confirm
    page = web.post(f"/admin/messages/{sent.id}/delete", data={"csrf": csrf}).text
    assert f"Deleted {sent.id}" in page
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_action_without_csrf_is_refused(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    make_session("desktop", "api")
    sent = mail.send(conn, a, "desktop/api", "keep me", None, clock.now())
    login(web)
    before = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
    posts = [
        ("/admin/machines", {"name": "vps"}),
        ("/admin/pairs", {"a": "laptop", "b": "desktop"}),
        ("/admin/pairs/delete", {"a": "laptop", "b": "desktop"}),
        ("/admin/machines/laptop/revoke", {"confirm": "laptop"}),
        (f"/admin/sessions/{a.id}/end", {}),
        (f"/admin/sessions/{a.id}/reset-depth", {}),
        (f"/admin/messages/{sent.id}/delete", {}),
    ]
    for path, form in posts:
        for csrf in (None, "wrong"):
            data = form if csrf is None else {**form, "csrf": csrf}
            assert web.post(path, data=data).status_code == 403, path
    assert conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == before


def test_stale_ids_give_a_banner(
    web: TestClient,
    conn: sqlite3.Connection,
    make_session: MakeSession,
    clock: FakeClock,
) -> None:
    a = make_session("laptop", "api")
    b = make_session("desktop", "api")
    sent = mail.send(conn, a, "desktop/api", "read", None, clock.now())
    mail.inbox(conn, b, 20, clock.now())
    csrf = login(web)
    cases = [
        ("/admin/sessions/999/end", {}, "No session 999"),
        ("/admin/sessions/abc/end", {}, "No session"),
        ("/admin/messages/nope/delete", {}, "No such message"),
        (f"/admin/messages/{sent.id}/delete", {}, "already read"),
        ("/admin/pairs", {"a": "laptop", "b": "ghost"}, "No machine named ghost"),
        ("/admin/machines/ghost/revoke", {"confirm": "ghost"}, "No machine named"),
        ("/admin/sessions/99999999999999999999999/end", {}, "No session"),
    ]
    for path, form, hint in cases:
        r = web.post(path, data={**form, "csrf": csrf}, follow_redirects=False)
        assert r.status_code == 303, path
        assert hint in web.get(r.headers["location"]).text, path


def test_refresh_does_not_repeat(web: TestClient, machines: Machines) -> None:
    csrf = login(web)
    form = {"csrf": csrf, "a": "laptop", "b": "desktop"}
    r = web.post("/admin/pairs/delete", data=form, follow_redirects=False)
    assert "Unpaired" in web.get(r.headers["location"]).text
    assert "Unpaired" not in web.get(r.headers["location"]).text


def test_stale_revoke_and_end_say_so(
    web: TestClient, conn: sqlite3.Connection, make_session: MakeSession
) -> None:
    s = make_session("laptop", "api")
    csrf = login(web)
    form = {"csrf": csrf, "confirm": "laptop"}
    web.post("/admin/machines/laptop/revoke", data=form)
    again = web.post("/admin/machines/laptop/revoke", data=form).text
    assert "already revoked" in again and "Revoked laptop" not in again
    for action in ("end", "reset-depth"):
        page = web.post(f"/admin/sessions/{s.id}/{action}", data={"csrf": csrf}).text
        assert "already ended" in page, action
    assert audit_rows(conn) == ["revoke"]


def test_new_key_and_post_result_pages_do_not_auto_refresh(
    web: TestClient, make_session: MakeSession
) -> None:
    # A refresh would wipe a key that is shown only once.
    make_session("laptop", "api")
    csrf = login(web)
    r = web.post(
        "/admin/machines", data={"csrf": csrf, "name": "vps"}, follow_redirects=False
    )
    key_page = web.get(r.headers["location"]).text
    assert "kmp_" in key_page and "data-auto-refresh" not in key_page
    assert "data-auto-refresh" in web.get("/admin/machines").text
    wrong = web.post(
        "/admin/machines/laptop/revoke", data={"csrf": csrf, "confirm": "lap"}
    ).text
    assert "did not match" in wrong and "data-auto-refresh" not in wrong
    expired = web.post("/admin/machines", data={"csrf": "nope", "name": "x"})
    assert expired.status_code == 403 and "data-auto-refresh" not in expired.text
