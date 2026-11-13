"""The docs must keep up with the code: tools, limits, settings, retired terms.

These catch the mechanical drift on every push. A full docs-vs-code review
still runs before each release; these are what it should never have to find.
"""

import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastmcp import Client

from kampung_server import limits as L
from kampung_server.app import build_server
from kampung_server.deps import ServerDeps
from kampung_server.guard import GuardSettings
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


def _names_tool(text: str, name: str) -> bool:
    """`name` as a code span of its own, or a call such as `name(...)`."""
    return re.search(rf"`{re.escape(name)}[`(]", text) is not None


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


async def _tool_names(tmp_path: Path) -> list[str]:
    from datetime import datetime, timezone

    deps = ServerDeps(
        tmp_path / "k.db", lambda: datetime.now(timezone.utc), RateLimiter()
    )
    async with Client(build_server(deps)) as c:
        return sorted(t.name for t in await c.list_tools())


def test_a_tool_name_inside_a_longer_name_does_not_count() -> None:
    assert not _names_tool("call `sender` or `inboxes`", "send")
    assert not _names_tool("call `inboxes`", "inbox")
    assert _names_tool("call `send(to, body)` then `inbox`", "send")
    assert _names_tool("call `inbox`", "inbox")


async def test_every_registered_tool_is_documented(tmp_path: Path) -> None:
    names = await _tool_names(tmp_path)
    assert names, "no tools registered"
    readme, reference = _read(ROOT / "README.md"), _read(
        ROOT / "docs" / "tool-reference.md"
    )
    page = _read(ROOT / "pages" / "index.html")
    for name in names:
        assert _names_tool(readme, name), f"README.md never names the {name} tool"
        assert _names_tool(reference, name), f"tool-reference.md never names {name}"
        assert f"<code>{name}</code>" in page, f"the landing page never names {name}"


def _thousands(n: int) -> str:
    return f"{n:,}"


def _minutes(span: timedelta) -> int:
    return int(span.total_seconds() // 60)


def _per(window: timedelta) -> str:
    """The table says "per minute"; any other window must change the text."""
    return "per minute" if window == timedelta(minutes=1) else f"per {window}"


WORDS = {1: "one", 2: "two", 3: "three", 4: "four"}


SECURITY_PHRASES = {
    "MAX_BODY_BYTES": f"{L.MAX_BODY_BYTES // 1024} KB of UTF-8",
    "MESSAGE_TTL": f"| Unread mail kept | {L.MESSAGE_TTL.days} days |",
    "SEND_RATE": f"| Sends per machine | {L.SEND_RATE} {_per(L.SEND_RATE_WINDOW)} |",
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
    "MAX_BRANCH_CHARS": f"| Session `branch` | {L.MAX_BRANCH_CHARS} printable",
    "STREAM_CONNECT_RATE": f"| Event streams per machine | {L.STREAM_CONNECT_RATE} "
    "connects per minute",
    "MAX_STREAMS": f"{L.MAX_LIVE_SESSIONS_PER_MACHINE} open at once",
    "MAX_SUMMARY_CHARS": f"| Session `summary` | {L.MAX_SUMMARY_CHARS} printable",
    "SUMMARY_RATE": f"{L.SUMMARY_RATE} updates per machine per minute",
    "MAX_CLIENT_VERSION_CHARS": f"| Session `client_version` | "
    f"{L.MAX_CLIENT_VERSION_CHARS} characters",
    "ENDED_SESSION_RETENTION": f"| Ended sessions kept | "
    f"{L.ENDED_SESSION_RETENTION.days} days",
    "AUTH_FAILURE_LIMIT": (
        f"{L.AUTH_FAILURE_LIMIT} within a minute"
        if L.AUTH_FAILURE_WINDOW == timedelta(minutes=1)
        else f"{L.AUTH_FAILURE_LIMIT} within {L.AUTH_FAILURE_WINDOW}"
    ),
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
    "CONNECTOR_WAKE": f"{GuardSettings().connector_wake_budget} per receiving "
    f"mailbox per {int(L.CONNECTOR_WAKE_WINDOW.total_seconds() // 60)} minutes "
    "(`KAMPUNG_CONNECTOR_WAKE_BUDGET`)",
    "OAUTH_REQUESTS": f"{L.OAUTH_REQUESTS_PER_CLIENT} per client address",
    "PEEK_WINDOWS": f"({L.PEEK_RATE // 30} windows polling every 2 seconds)",
    "IMAGE_DECODES": f"{WORDS.get(L.IMAGE_DECODES, L.IMAGE_DECODES)} decoded at a time",
    "IMAGE_DECODE_WAIT": f"waits {L.IMAGE_DECODE_WAIT:g} seconds for a decode",
    "MIN_FREE_DISK_BYTES": f"under {L.MIN_FREE_DISK_BYTES // 2**30} GB free",
    "SESSION_DELETE_BATCH": f"days, then deleted up to "
    f"{_thousands(L.SESSION_DELETE_BATCH)} per cleanup pass",
    "AUDIT_DELETE_BATCH": f"the oldest deleted up to "
    f"{_thousands(L.AUDIT_DELETE_BATCH)} per cleanup pass",
    "NOHOOK_MAILBOX_REFILL": f"they come back after "
    f"{_minutes(L.NOHOOK_MAILBOX_REFILL)} minutes without waking sends",
    "OAUTH_CONNECT_CODE_TTL": f"| Connect code and pending sign-in | "
    f"{_minutes(L.OAUTH_CONNECT_CODE_TTL)} minutes, single use",
    "OAUTH_REQUEST_TTL": f"{_minutes(L.OAUTH_REQUEST_TTL)} minutes, single use",
    "OAUTH_CONNECT_ATTEMPTS": f"{L.OAUTH_CONNECT_ATTEMPTS} wrong codes burn",
    "CONNECT_FAILURE": f"{L.CONNECT_FAILURE_LIMIT} wrong codes per client address "
    f"in {_minutes(L.CONNECT_FAILURE_WINDOW)} minutes",
    "OAUTH_MAX_REQUESTS": f"and {L.OAUTH_MAX_REQUESTS} in all",
    "OAUTH_CODE_TTL": f"authorization code {_minutes(L.OAUTH_CODE_TTL)} minutes",
    "OAUTH_ACCESS_TTL": f"access {_minutes(L.OAUTH_ACCESS_TTL) // 60} hour",
    "OAUTH_REFRESH_TTL": f"refresh {L.OAUTH_REFRESH_TTL.days} days renewed on use",
    "OAUTH_REFRESH_REUSE": f"{int(L.OAUTH_REFRESH_REUSE.total_seconds())} seconds "
    "of refresh retry",
    "OAUTH_ROTATED_RETENTION": f"stay on record {L.OAUTH_ROTATED_RETENTION.days} days",
    "MAX_OAUTH_BODY_BYTES": f"{L.MAX_OAUTH_BODY_BYTES // 1024} KB on `/token`",
    "OWNER_INSTRUCTION_TTL": f"| Owner instruction read deadline | "
    f"{_minutes(L.OWNER_INSTRUCTION_TTL)} minutes",
    "OWNER_BIND_WINDOW": f"seen within {_minutes(L.OWNER_BIND_WINDOW)} minutes "
    "if it checks for mail every 2 seconds",
    "OWNER_CODEX_TYPED_WINDOW": f"a Codex session typed in within "
    f"{_minutes(L.OWNER_CODEX_TYPED_WINDOW)} minutes",
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
