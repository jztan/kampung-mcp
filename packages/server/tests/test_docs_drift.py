"""The docs must keep up with the code: tools, limits, settings, retired terms.

These catch the mechanical drift on every push. A full docs-vs-code review
still runs before each release; these are what it should never have to find.
"""

import re
from pathlib import Path

import pytest
from fastmcp import Client

from kampung_server import limits as L
from kampung_server.app import build_server
from kampung_server.deps import ServerDeps
from kampung_server.ratelimit import RateLimiter

ROOT = Path(__file__).resolve().parents[3]
SERVER_SRC = ROOT / "packages" / "server" / "src"
CLIENT_SRC = ROOT / "packages" / "client" / "src"
SETTING = re.compile(r"KAMPUNG_[A-Z][A-Z_]*[A-Z]")
# Prose that describes the current product. CHANGELOG is history, and the
# client README is regenerated from the root README at each release.
PROSE = [
    ROOT / "README.md",
    ROOT / "SECURITY.md",
    ROOT / "roadmap.md",
    *sorted((ROOT / "docs").glob("*.md")),
    ROOT / "pages" / "index.html",
]
RETIRED = [
    r"depth",
    r"not_owner",
    r"three (MCP )?tools",
    r"reset[ -]depth",
]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def _tool_names(tmp_path: Path) -> list[str]:
    from datetime import datetime, timezone

    deps = ServerDeps(
        tmp_path / "k.db", lambda: datetime.now(timezone.utc), RateLimiter()
    )
    async with Client(build_server(deps)) as c:
        return sorted(t.name for t in await c.list_tools())


async def test_every_registered_tool_is_documented(tmp_path: Path) -> None:
    names = await _tool_names(tmp_path)
    assert names, "no tools registered"
    readme, reference = _read(ROOT / "README.md"), _read(
        ROOT / "docs" / "tool-reference.md"
    )
    page = _read(ROOT / "pages" / "index.html")
    for name in names:
        assert f"`{name}" in readme, f"README.md never names the {name} tool"
        assert f"`{name}" in reference, f"tool-reference.md never names {name}"
        assert f"<code>{name}</code>" in page, f"the landing page never names {name}"


def _thousands(n: int) -> str:
    return f"{n:,}"


SECURITY_PHRASES = {
    "MAX_BODY_BYTES": f"{L.MAX_BODY_BYTES // 1024} KB of UTF-8",
    "MESSAGE_TTL": f"| Unread mail kept | {L.MESSAGE_TTL.days} days |",
    "SEND_RATE": f"| Sends per machine | {L.SEND_RATE} per minute |",
    "REGISTER_RATE": f"| Session registrations per machine | {L.REGISTER_RATE} per",
    "PEEK_RATE": f"`peek` {_thousands(L.PEEK_RATE)} per minute",
    "END_RATE": f"`end` {L.END_RATE} per minute",
    "HUMAN_TURN_RATE": f"| Typed-prompt calls per machine | {L.HUMAN_TURN_RATE} per",
    "AUDIT_RETENTION": f"{L.AUDIT_RETENTION.days} days and at most "
    f"{_thousands(L.AUDIT_MAX_ROWS)} rows",
    "MAX_WAITING_PER_MAILBOX": f"| Unread mail per mailbox | "
    f"{_thousands(L.MAX_WAITING_PER_MAILBOX)} (",
    "MAX_WAITING_PER_SENDER": f"from one sending machine | "
    f"{L.MAX_WAITING_PER_SENDER} (",
    "MAX_LIVE_SESSIONS": f"{L.MAX_LIVE_SESSIONS_PER_MAILBOX} per mailbox, "
    f"{L.MAX_LIVE_SESSIONS_PER_MACHINE} per machine",
    "MAX_MAILBOXES_PER_MACHINE": f"| Mailboxes per machine | "
    f"{_thousands(L.MAX_MAILBOXES_PER_MACHINE)} |",
    "MAX_CWD_CHARS": f"{_thousands(L.MAX_CWD_CHARS)} printable characters",
    "ENDED_SESSION_RETENTION": f"| Ended sessions kept | "
    f"{L.ENDED_SESSION_RETENTION.days} days",
    "AUTH_FAILURE_LIMIT": f"{L.AUTH_FAILURE_LIMIT} within a minute",
    "MAX_ATTACHMENT_TEXT_BYTES": f"text up to {L.MAX_ATTACHMENT_TEXT_BYTES // 1024} KB",
    "MAX_ATTACHMENT_IMAGE_BYTES": f"PNG or JPEG up to "
    f"{L.MAX_ATTACHMENT_IMAGE_BYTES // 1024} KB",
    "IMAGE_LONG_EDGE": f"{L.IMAGE_LONG_EDGE} pixels on the long edge",
    "IMAGE_MAX_PIXELS": f"{L.IMAGE_MAX_PIXELS // 1_000_000} million pixels",
    "READ_SLICE": f"{L.READ_SLICE_DEFAULT // 1024} KB per slice by default, "
    f"{L.READ_SLICE_MAX // 1024} KB at most",
    "UNREAD_ATTACHMENTS": f"{L.MAX_UNREAD_ATTACHMENT_BYTES_PER_MACHINE // 2**20} MB "
    f"per sending machine; {L.MAX_ATTACHMENT_BYTES_TOTAL // 2**20} MB",
    "MAX_MCP_BODY_BYTES": f"| `/mcp` request body | {L.MAX_MCP_BODY_BYTES // 2**20} MB",
    "MAX_REQUEST_BYTES": f"| `/v1` request body | {L.MAX_REQUEST_BYTES // 1024} KB",
    "CONNECTOR_WAKE": f"{L.CONNECTOR_WAKE_BUDGET} per connector session per "
    f"{int(L.CONNECTOR_WAKE_WINDOW.total_seconds() // 60)} minutes",
    "OAUTH_REQUESTS": f"{L.OAUTH_REQUESTS_PER_CLIENT} per client address",
}


@pytest.mark.parametrize("name", sorted(SECURITY_PHRASES))
def test_security_limits_table_matches_limits_py(name: str) -> None:
    phrase = SECURITY_PHRASES[name]
    assert phrase in _read(ROOT / "SECURITY.md"), (
        f"SECURITY.md's Limits table is out of date for {name}: " f"expected {phrase!r}"
    )


def _settings(src: Path) -> set[str]:
    found: set[str] = set()
    for path in src.rglob("*.py"):
        found |= set(SETTING.findall(_read(path)))
    return found


def test_every_server_setting_is_documented() -> None:
    doc = _read(ROOT / "docs" / "server.md")
    missing = sorted(s for s in _settings(SERVER_SRC) if s not in doc)
    assert not missing, f"docs/server.md does not document {missing}"


def test_every_client_setting_is_documented() -> None:
    doc = _read(ROOT / "docs" / "clients.md")
    missing = sorted(s for s in _settings(CLIENT_SRC) if s not in doc)
    assert not missing, f"docs/clients.md does not document {missing}"


@pytest.mark.parametrize("path", PROSE, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_retired_terms_in_docs(path: Path) -> None:
    hits = []
    for n, line in enumerate(_read(path).splitlines(), 1):
        for term in RETIRED:
            if re.search(rf"\b{term}\b", line, re.IGNORECASE):
                hits.append(f"{path.name}:{n}: {term}")
    assert not hits, "retired terms in current docs: " + "; ".join(hits)
