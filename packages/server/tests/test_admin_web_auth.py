from collections.abc import Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from admin_web_testlib import TOKEN, csrf_of, login
from kampung_server.app import create_app
from kampung_server.deps import ServerDeps
from kampung_server.models import Machine
from kampung_server.ratelimit import RateLimiter
from server_testlib import FakeClock

CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self'; "
    "img-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
    "base-uri 'none'"
)


def client_with(db_path: Path, clock: FakeClock, token: str | None) -> TestClient:
    deps = ServerDeps(db_path, clock.now, RateLimiter(), admin_token=token)
    return TestClient(create_app(deps), base_url="https://testserver")


@pytest.fixture
def no_token(db_path: Path, conn: object, clock: FakeClock) -> Iterator[TestClient]:
    with client_with(db_path, clock, None) as c:
        yield c


def test_not_mounted_without_token(no_token: TestClient) -> None:
    assert no_token.get("/admin", follow_redirects=False).status_code == 404
    assert no_token.get("/admin/login").status_code == 404
    assert no_token.get("/health").status_code == 200


def test_short_token_is_refused(
    db_path: Path, conn: object, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    with client_with(db_path, clock, "short") as c:
        assert c.get("/admin/login").status_code == 404
    assert "at least 24" in capsys.readouterr().err


def test_admin_redirects_to_login(web: TestClient) -> None:
    r = web.get("/admin", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/login"


def test_login_page_explains_https(web: TestClient) -> None:
    r = web.get("/admin/login")
    assert r.status_code == 200
    assert "HTTPS" in r.text and "http://localhost" in r.text


def test_wrong_token_is_rejected(web: TestClient) -> None:
    r = web.post("/admin/login", data={"token": "x"}, follow_redirects=False)
    assert r.status_code == 401
    assert "kampung_admin" not in r.headers.get("set-cookie", "")


def test_login_sets_strict_cookie(web: TestClient) -> None:
    r = web.post("/admin/login", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    cookie = r.headers["set-cookie"].lower()
    for part in ("httponly", "secure", "samesite=strict", "path=/admin"):
        assert part in cookie
    assert "max-age=43200" in cookie


def http_client(admin_deps: ServerDeps, host: str) -> TestClient:
    return TestClient(create_app(admin_deps), base_url=f"http://{host}")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_loopback_http_login_works(admin_deps: ServerDeps, host: str) -> None:
    # Safari drops Secure cookies on http://localhost, and so does httpx.
    with http_client(admin_deps, host) as c:
        r = c.post("/admin/login", data={"token": TOKEN}, follow_redirects=False)
        assert "secure" not in r.headers["set-cookie"].lower()
        assert c.get("/admin", follow_redirects=False).status_code == 200


def test_lan_http_cookie_stays_secure(admin_deps: ServerDeps) -> None:
    with http_client(admin_deps, "192.168.1.5") as c:
        r = c.post("/admin/login", data={"token": TOKEN}, follow_redirects=False)
        assert "secure" in r.headers["set-cookie"].lower()


def test_loopback_http_logout_clears_cookie(admin_deps: ServerDeps) -> None:
    with http_client(admin_deps, "localhost") as c:
        csrf = login(c)
        r = c.post("/admin/logout", data={"csrf": csrf}, follow_redirects=False)
        assert r.status_code == 303
        assert "secure" not in r.headers["set-cookie"].lower()
        assert "kampung_admin" not in c.cookies


def test_session_expires_after_12_hours(web: TestClient, clock: FakeClock) -> None:
    login(web)
    assert web.get("/admin", follow_redirects=False).status_code == 200
    clock.advance(hours=12, seconds=1)
    r = web.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_login_rate_limited(web: TestClient) -> None:
    for _ in range(5):
        assert web.post("/admin/login", data={"token": "x"}).status_code == 401
    r = web.post("/admin/login", data={"token": TOKEN})
    assert r.status_code == 429
    assert "Too many attempts" in r.text


def test_logout_ends_session(web: TestClient) -> None:
    csrf = login(web)
    r = web.post("/admin/logout", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303
    r = web.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_logout_needs_csrf(web: TestClient) -> None:
    login(web)
    assert web.post("/admin/logout", data={"csrf": "nope"}).status_code == 403
    assert web.get("/admin", follow_redirects=False).status_code == 200


def test_security_headers(web: TestClient) -> None:
    for path in (
        "/admin/login",
        "/admin",
        "/admin/static/admin.css",
        "/admin/static/admin.js",
    ):
        r = web.get(path, follow_redirects=False)
        assert r.headers["content-security-policy"] == CSP, path
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-store"


def test_csp_allows_only_the_same_origin_script() -> None:
    assert "'unsafe-inline'" not in CSP and "'unsafe-eval'" not in CSP


def test_auto_refresh_script_served(web: TestClient) -> None:
    r = web.get("/admin/static/admin.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert r.headers["content-security-policy"] == CSP
    assert "data-auto-refresh" in r.text or "autoRefresh" in r.text


def test_stylesheet_served(web: TestClient) -> None:
    r = web.get("/admin/static/admin.css")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/css")
    assert "--accent" in r.text


def test_machine_key_does_not_open_admin(
    web: TestClient, machines: dict[str, tuple[Machine, str]]
) -> None:
    key = machines["laptop"][1]
    r = web.get(
        "/admin", headers={"Authorization": f"Bearer {key}"}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_signed_in_page_has_csrf_field(web: TestClient) -> None:
    login(web)
    assert csrf_of(web.get("/admin").text)
