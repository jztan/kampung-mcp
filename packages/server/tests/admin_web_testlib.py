"""Helpers for the admin dashboard tests."""

import re

from starlette.testclient import TestClient

TOKEN = "t" * 32
CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def csrf_of(html: str) -> str:
    m = CSRF.search(html)
    assert m, "no csrf field on the page"
    return m.group(1)


def login(client: TestClient) -> str:
    """Sign in and return the session's CSRF token."""
    r = client.post("/admin/login", data={"token": TOKEN})
    assert r.status_code == 200, r.text
    return csrf_of(r.text)
