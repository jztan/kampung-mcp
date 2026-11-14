"""The per-session event stream: peek counts pushed as they change."""

import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from kampung_server import db, events, mail
from kampung_server.models import load_session
from server_testlib import LiveServer as Server
from server_testlib import live_server
from server_testlib import register_session as register

Event = tuple[str, dict[str, object]]


class Reader:
    """Reads one stream in a thread and keeps every event it sees."""

    def __init__(self, url: str, key: str) -> None:
        self.events: list[Event] = []
        self.status: int | None = None
        self.body = b""
        self._done = threading.Event()
        self._client = httpx.Client(timeout=httpx.Timeout(5, read=10))
        self._thread = threading.Thread(target=self._run, args=(url, key), daemon=True)
        self._thread.start()

    def _run(self, url: str, key: str) -> None:
        try:
            with self._client.stream(
                "GET", url, headers={"Authorization": f"Bearer {key}"}
            ) as r:
                self.status = r.status_code
                if r.status_code != 200:
                    self.body = r.read()
                    return
                name = ""
                for line in r.iter_lines():
                    if line.startswith("event: "):
                        name = line[len("event: ") :]
                    elif line.startswith("data: "):
                        self.events.append((name, json.loads(line[len("data: ") :])))
        except (httpx.HTTPError, RuntimeError):
            pass
        finally:
            self._done.set()

    def wait_for(self, count: int, seconds: float = 3.0) -> list[Event]:
        end = time.monotonic() + seconds
        while len(self.events) < count and time.monotonic() < end:
            time.sleep(0.02)
        return list(self.events)

    def wait_closed(self, seconds: float = 3.0) -> bool:
        return self._done.wait(seconds)

    def close(self) -> None:
        self._client.close()


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "SIGNAL_CHECK", 0.02)
    monkeypatch.setattr(events, "PING_EVERY", 0.5)
    monkeypatch.setattr(events, "RECHECK", 5.0)


@pytest.fixture
def server(tmp_path: Path) -> Iterator[Server]:
    with live_server(tmp_path) as live:
        yield live


@pytest.fixture
def readers() -> Iterator[list[Reader]]:
    opened: list[Reader] = []
    yield opened
    for r in opened:
        r.close()


def _open(server: Server, machine: str, sid: str, readers: list[Reader]) -> Reader:
    r = Reader(f"{server.url}/v1/sessions/{sid}/events", server.keys[machine])
    readers.append(r)
    return r


def _send(server: Server, sender: str, to: str) -> None:
    conn = db.connect(server.deps.db_path)
    try:
        s = load_session(conn, int(sender))
        assert s is not None
        mail.send(conn, s, to, "hello", None, server.deps.clock())
    finally:
        conn.close()


def test_connect_sends_the_counts_at_once(
    server: Server, readers: list[Reader]
) -> None:
    sid = register(server, "desktop", "blog")
    r = _open(server, "desktop", sid, readers)
    assert r.wait_for(1)[0] == ("peek", {"waiting": 0, "wakeable": 0, "newest": None})


def test_a_send_pushes_new_counts_quickly(
    server: Server, readers: list[Reader]
) -> None:
    sid = register(server, "desktop", "blog")
    sender = register(server, "laptop", "api")
    r = _open(server, "desktop", sid, readers)
    r.wait_for(1)
    start = time.monotonic()
    _send(server, sender, "desktop/blog")
    got = r.wait_for(2)
    assert time.monotonic() - start < 1.0
    name, body = got[1]
    assert name == "peek" and body["waiting"] == 1 and body["wakeable"] == 1
    assert isinstance(body["newest"], str)


def test_reading_the_inbox_pushes_the_drop(
    server: Server, readers: list[Reader]
) -> None:
    sid = register(server, "desktop", "blog")
    sender = register(server, "laptop", "api")
    _send(server, sender, "desktop/blog")
    r = _open(server, "desktop", sid, readers)
    assert r.wait_for(1)[0][1]["waiting"] == 1
    conn = db.connect(server.deps.db_path)
    try:
        s = load_session(conn, int(sid))
        assert s is not None
        mail.inbox(conn, s, 20, server.deps.clock())
    finally:
        conn.close()
    assert r.wait_for(2)[1] == ("peek", {"waiting": 0, "wakeable": 0, "newest": None})


def test_no_event_when_nothing_changed(server: Server, readers: list[Reader]) -> None:
    sid = register(server, "desktop", "blog")
    other = register(server, "laptop", "api")
    r = _open(server, "desktop", sid, readers)
    r.wait_for(1)
    _send(server, sid, "laptop/api")  # mail for someone else bumps the signal
    time.sleep(0.5)
    assert len(r.events) == 1
    assert other


def test_ended_closes_the_stream(server: Server, readers: list[Reader]) -> None:
    from kampung_server.admin import AdminService

    sid = register(server, "desktop", "blog")
    r = _open(server, "desktop", sid, readers)
    r.wait_for(1)
    admin = AdminService(lambda: db.connect(server.deps.db_path), server.deps.clock)
    admin.end_session(int(sid), "admin:cli")
    assert r.wait_for(2)[-1] == ("ended", {})
    assert r.wait_closed()


def test_refusals_match_peek(server: Server, readers: list[Reader]) -> None:
    sid = register(server, "desktop", "blog")
    r = _open(server, "laptop", sid, readers)  # another machine's session
    r.wait_closed()
    assert r.status == 404 and json.loads(r.body)["error"] == "unknown_session"
    no_key = httpx.get(f"{server.url}/v1/sessions/{sid}/events", timeout=5)
    assert no_key.status_code == 401


def test_open_streams_are_capped_per_machine(
    server: Server, readers: list[Reader], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(events, "MAX_STREAMS_PER_MACHINE", 2)
    a = register(server, "desktop", "blog")
    b = register(server, "desktop", "web")
    c = register(server, "desktop", "docs")
    for sid in (a, b):
        _open(server, "desktop", sid, readers).wait_for(1)
    third = _open(server, "desktop", c, readers)
    third.wait_closed()
    assert third.status == 429
    assert json.loads(third.body)["error"] == "too_many_streams"


def test_the_stream_never_touches_last_seen(
    server: Server, readers: list[Reader]
) -> None:
    sid = register(server, "desktop", "blog")
    conn = db.connect(server.deps.db_path)
    before = conn.execute(
        "SELECT last_seen FROM sessions WHERE id = ?", (int(sid),)
    ).fetchone()[0]
    r = _open(server, "desktop", sid, readers)
    r.wait_for(1)
    time.sleep(1.2)  # past two pings
    after = conn.execute(
        "SELECT last_seen FROM sessions WHERE id = ?", (int(sid),)
    ).fetchone()[0]
    conn.close()
    assert after == before


def test_peek_counts_writes_nothing(server: Server) -> None:
    sid = register(server, "desktop", "blog")
    conn = db.connect(server.deps.db_path)
    try:
        s = load_session(conn, int(sid))
        assert s is not None
        before = conn.total_changes
        result = mail.peek_counts(conn, s, server.deps.clock())
        assert conn.total_changes == before
        assert (result.waiting, result.wakeable) == (0, 0)
    finally:
        conn.close()


def test_peek_counts_reads_one_snapshot(server: Server) -> None:
    """A send landing between the count and the newest id must not give
    wakeable 0 with a newest id: the adapter would remember that id as
    announced and never wake for it."""
    sid = register(server, "desktop", "blog")
    sender = register(server, "laptop", "api")
    conn = db.connect(server.deps.db_path)
    calls = 0

    class SendsBetweenQueries:
        def execute(self, *args: object) -> object:
            nonlocal calls
            result = conn.execute(*args)  # type: ignore[arg-type]
            calls += 1
            if calls == 1:
                _send(server, sender, "desktop/blog")
            return result

    try:
        s = load_session(conn, int(sid))
        assert s is not None
        got = mail.peek_counts(
            SendsBetweenQueries(), s, server.deps.clock()  # type: ignore[arg-type]
        )
        assert (got.wakeable == 0) == (got.newest is None)
    finally:
        conn.close()


def test_a_stream_ends_when_its_key_stops_working(
    server: Server, readers: list[Reader]
) -> None:
    """A key rotated by the CLI (another process, so no in-memory bump) must
    not keep feeding a stream opened with the old key, even if the session
    was revived before the stream looked."""
    from kampung_server import keys as keymod
    from kampung_server.mailsignal import SIGNAL

    sid = register(server, "desktop", "blog")
    r = _open(server, "desktop", sid, readers)
    r.wait_for(1)
    conn = db.connect(server.deps.db_path)
    try:
        conn.execute(
            "UPDATE machines SET key_hash = ? WHERE name = 'desktop'",
            (keymod.hash_key("a-new-key-the-stream-never-had"),),
        )
    finally:
        conn.close()
    SIGNAL.bump()
    assert r.wait_closed()


async def test_a_stream_that_never_starts_frees_its_slot(server: Server) -> None:
    """A client gone before the body starts (the first send fails) must not
    keep a stream slot: 256 such connects would lock the machine out."""
    from kampung_server.app import create_app

    sid = register(server, "desktop", "blog")
    app = create_app(server.deps)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/v1/sessions/{sid}/events",
        "raw_path": f"/v1/sessions/{sid}/events".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {server.keys['desktop']}".encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }

    async def receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        raise OSError("client went away")

    with pytest.raises(Exception):
        await app(scope, receive, send)
    conn = db.connect(server.deps.db_path)
    try:
        mid = conn.execute("SELECT id FROM machines WHERE name = 'desktop'").fetchone()[
            0
        ]
    finally:
        conn.close()
    assert events._open.get(mid, 0) == 0
