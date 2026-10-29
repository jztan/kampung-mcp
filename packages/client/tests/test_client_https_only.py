"""The machine key never travels over plain http, except to loopback or
Tailscale hosts, and never follows a redirect off the configured server."""

import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastmcp import Client

from client_testlib import LiveServer, hook_chain, run_hook, write_config
from kampung_client import cli, hook, setup
from kampung_client.adapter import build_adapter
from kampung_client.setup import SetupError, run_setup

ACCEPTED = [
    "https://kampung.example.com",
    "https://192.168.1.10:8803",
    "http://localhost:8803",
    "http://127.0.0.1:8803",
    "http://127.9.9.9",
    "http://[::1]:8803",
    "http://100.101.102.103:8803",
    "http://100.64.0.1",
    "http://100.127.255.254",
    "http://[fd7a:115c:a1e0::1]:8803",
    "http://box.tail1234.ts.net",
    "HTTP://Box.Tail1234.TS.NET:8803",
]
REJECTED = [
    ("http://192.168.1.10:8803", "192.168.1.10"),
    ("http://kampung.example.com", "kampung.example.com"),
    ("http://100.128.0.1", "100.128.0.1"),
    ("http://100.63.255.255", "100.63.255.255"),
    ("http://[fd7a:115c:a1e1::1]", "fd7a:115c:a1e1::1"),
    ("http://ts.net", "ts.net"),
    ("http://evil.ts.net.example.com", "evil.ts.net.example.com"),
    ("http://localhost.example.com", "localhost.example.com"),
    ("http://[::ffff:127.0.0.1]", "::ffff:127.0.0.1"),
]
REFUSAL = "Refusing to send the key over plain http to {host}"


def config_file(home: Path) -> Path:
    return home / ".config" / "kampung" / "config.json"


def write_raw_config(home: Path, data: dict[str, object]) -> None:
    config_file(home).parent.mkdir(parents=True, exist_ok=True)
    config_file(home).write_text(json.dumps(data))


# The URL rule ---------------------------------------------------------------


@pytest.mark.parametrize("url", ACCEPTED)
def test_url_accepted(url: str) -> None:
    assert hook.server_url_problem(url) is None


@pytest.mark.parametrize("url,host", REJECTED)
def test_plain_http_to_other_hosts_is_refused(url: str, host: str) -> None:
    problem = hook.server_url_problem(url)
    assert problem is not None
    assert problem.startswith(REFUSAL.format(host=host))
    assert "https://" in problem and "Tailscale" in problem


@pytest.mark.parametrize(
    "url", ["kampung.example.com", "ftp://kampung.example.com", "http://", ""]
)
def test_other_schemes_and_missing_hosts_are_refused(url: str) -> None:
    assert hook.server_url_problem(url) is not None


def test_allow_insecure_http_only_lifts_the_http_rule() -> None:
    assert hook.server_url_problem("http://192.168.1.10", True) is None
    assert hook.server_url_problem("ftp://192.168.1.10", True) is not None


# Config loading (hook and adapter) -------------------------------------------


def test_load_config_refuses_plain_http(home: Path) -> None:
    write_config(home, "http://192.168.1.10:8803", "kmp_x")
    with pytest.raises(hook.InsecureServerError, match=REFUSAL.format(host="")):
        hook.load_config()


def test_load_config_allows_plain_http_when_opted_in(home: Path) -> None:
    write_raw_config(
        home,
        {"server": "http://192.168.1.10/", "key": "kmp_x", "allow_insecure_http": True},
    )
    assert hook.load_config() == {"server": "http://192.168.1.10", "key": "kmp_x"}


def test_load_config_opt_in_must_be_true(home: Path) -> None:
    write_raw_config(
        home,
        {"server": "http://192.168.1.10", "key": "kmp_x", "allow_insecure_http": "1"},
    )
    with pytest.raises(hook.InsecureServerError):
        hook.load_config()


def test_hook_refuses_plain_http_config_and_exits_0(home: Path, tmp_path: Path) -> None:
    write_config(home, "http://192.168.1.10:8803", "kmp_x")
    event = {
        "hook_event_name": "SessionStart",
        "session_id": "cc-1",
        "cwd": str(tmp_path),
        "source": "startup",
    }
    r = run_hook(home, event)
    assert r.returncode == 0 and r.stdout == ""
    log = (home / ".cache" / "kampung" / "hooks.log").read_text()
    assert REFUSAL.format(host="192.168.1.10") in log
    assert hook.read_session_file("cc-1") is None


def test_hook_refuses_before_any_request(home: Path, tmp_path: Path) -> None:
    httpd, hits = recording_server()
    port = httpd.server_address[1]
    try:
        # 127.1 reaches this server, but the host is checked as written, and
        # only the dotted-quad form counts as loopback.
        write_config(home, f"http://127.1:{port}", "kmp_x")
        r = run_hook(
            home,
            {
                "hook_event_name": "SessionStart",
                "session_id": "cc-1",
                "cwd": str(tmp_path),
                "source": "startup",
            },
        )
    finally:
        httpd.shutdown()
    assert r.returncode == 0 and r.stdout == "" and hits == []


def test_adapter_refuses_to_start_with_plain_http_config(home: Path) -> None:
    write_config(home, "http://192.168.1.10:8803", "kmp_x")
    r = subprocess.run(
        [sys.executable, "-m", "kampung_client", "adapter"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=30,
    )
    assert r.returncode == 1
    assert REFUSAL.format(host="192.168.1.10") in r.stderr


def test_cli_adapter_refuses_in_process(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_config(home, "http://kampung.example.com", "kmp_x")
    assert cli.main(["adapter"]) == 1
    assert REFUSAL.format(host="kampung.example.com") in capsys.readouterr().err


def test_adapter_warns_when_insecure_http_is_allowed(home: Path) -> None:
    write_raw_config(
        home,
        {"server": "http://192.168.1.10", "key": "kmp_x", "allow_insecure_http": True},
    )
    r = subprocess.run(
        [sys.executable, "-m", "kampung_client", "adapter"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=30,
    )
    assert r.returncode == 0
    assert "allow_insecure_http" in r.stderr and "192.168.1.10" in r.stderr


async def test_adapter_tool_call_refuses_a_config_edited_to_plain_http(
    home: Path,
) -> None:
    write_config(home, "http://127.0.0.1:9", "kmp_x")
    async with Client(build_adapter("claude-code", "/w/api", wait=0)) as c:
        write_config(home, "http://192.168.1.10:8803", "kmp_x")
        r = await c.call_tool("inbox", {})
    assert r.data["error"] == "insecure_server_url"
    assert REFUSAL.format(host="192.168.1.10") in r.data["hint"]


# Setup ----------------------------------------------------------------------


class NoRequests:
    """Stands in for setup's HTTP call; fails the test if anything is sent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, url: str, key: str | None = None) -> int:
        self.calls.append((url, key))
        raise AssertionError(f"setup made a request to {url}")


@pytest.mark.parametrize("url,host", REJECTED[:3])
def test_setup_refuses_plain_http_before_sending_the_key(
    home: Path, monkeypatch: pytest.MonkeyPatch, url: str, host: str
) -> None:
    write_config(home, "https://old.example.com", "kmp_old")
    sent = NoRequests()
    monkeypatch.setattr(setup, "_status", sent)
    with pytest.raises(SetupError, match=REFUSAL.format(host=host)):
        run_setup(url, "kmp_new", ["claude-code"], lambda cmd: 0)
    assert sent.calls == []
    assert json.loads(config_file(home).read_text())["key"] == "kmp_old"


def test_setup_refusal_reaches_no_server(home: Path) -> None:
    httpd, hits = recording_server()
    try:
        with pytest.raises(SetupError, match=REFUSAL.format(host="127.1")):
            run_setup(
                f"http://127.1:{httpd.server_address[1]}",
                "kmp_x",
                ["codex"],
                lambda cmd: 0,
            )
    finally:
        httpd.shutdown()
    assert hits == []


def test_setup_accepts_loopback_by_name(home: Path, server: LiveServer) -> None:
    url = server.url.replace("127.0.0.1", "localhost")
    run_setup(url, server.keys["laptop"], ["codex"], lambda cmd: 0)
    assert json.loads(config_file(home).read_text()) == {
        "server": url,
        "key": server.keys["laptop"],
    }


def test_setup_allow_insecure_http_saves_flag_and_warns(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([200, 404])  # /health, then the keyed peek
    monkeypatch.setattr(setup, "_status", lambda url, key=None: next(answers))
    lines = run_setup(
        "http://192.168.1.10:8803",
        "kmp_x",
        ["codex"],
        lambda cmd: 0,
        allow_insecure_http=True,
    )
    assert json.loads(config_file(home).read_text()) == {
        "server": "http://192.168.1.10:8803",
        "key": "kmp_x",
        "allow_insecure_http": True,
    }
    assert lines[0].startswith("Warning:") and "plain http" in lines[0]
    assert hook.load_config()["server"] == "http://192.168.1.10:8803"


def test_cli_setup_allow_insecure_http_flag(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter([200, 404])
    monkeypatch.setattr(setup, "_status", lambda url, key=None: next(answers))
    monkeypatch.setattr(setup, "default_runner", lambda cmd: 0)
    argv = ["setup", "--server", "http://192.168.1.10", "--key", "kmp_x"]
    assert cli.main([*argv, "--allow-insecure-http"]) == 0
    assert "Warning:" in capsys.readouterr().out
    assert json.loads(config_file(home).read_text())["allow_insecure_http"] is True


def test_cli_setup_plain_http_without_flag_fails(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(setup, "_status", NoRequests())
    argv = ["setup", "--server", "http://192.168.1.10", "--key", "kmp_x"]
    assert cli.main(argv) == 1
    assert REFUSAL.format(host="192.168.1.10") in capsys.readouterr().err


# Redirects ------------------------------------------------------------------


def recording_server(
    redirect_to: str | None = None,
) -> tuple[ThreadingHTTPServer, list[str]]:
    """Answers /health with 200. Every other request is redirected with a 307
    to `redirect_to` when given, else answered 404. Records each request as
    "METHOD path auth=<yes|no>"."""
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def _handle(self) -> None:
            auth = "yes" if self.headers.get("Authorization") else "no"
            hits.append(f"{self.command} {self.path} auth={auth}")
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/health":
                status, headers = 200, {}
            elif redirect_to is not None:
                status, headers = 307, {"Location": redirect_to + self.path}
            else:
                status, headers = 404, {}
            body = b"{}"
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = do_DELETE = _handle

        def log_message(self, *args: object) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, hits


@pytest.fixture
def redirecting() -> Iterator[tuple[str, list[str], list[str]]]:
    """(server url, hits on the server, hits on the redirect target): every
    keyed request to the server is redirected to another origin, which must
    never see a request."""
    target, target_hits = recording_server()
    target_url = f"http://localhost:{target.server_address[1]}"
    source, source_hits = recording_server(redirect_to=target_url)
    yield f"http://127.0.0.1:{source.server_address[1]}", source_hits, target_hits
    source.shutdown()
    target.shutdown()


def test_hook_api_does_not_follow_redirects(
    home: Path, redirecting: tuple[str, list[str], list[str]]
) -> None:
    url, hits, target_hits = redirecting
    write_config(home, url, "kmp_x")
    status, _ = hook.api("GET", "/v1/sessions/1/peek")
    assert status == 307
    assert hits == ["GET /v1/sessions/1/peek auth=yes"] and target_hits == []


def test_setup_does_not_follow_redirects_with_the_key(
    home: Path, redirecting: tuple[str, list[str], list[str]]
) -> None:
    url, hits, target_hits = redirecting
    with pytest.raises(SetupError, match="HTTP 307"):
        run_setup(url, "kmp_x", ["codex"], lambda cmd: 0)
    assert hits[-1] == "GET /v1/sessions/0/peek auth=yes" and target_hits == []


async def test_adapter_does_not_follow_redirects_off_the_server(
    home: Path, redirecting: tuple[str, list[str], list[str]]
) -> None:
    url, hits, target_hits = redirecting
    write_config(home, url, "kmp_x")
    hook.write_session_file(
        "cc-1",
        {
            "session": "1",
            "hooks": True,
            "cwd": "/w/api",
            "agent": "claude-code",
            "ancestors": hook_chain(),
        },
    )
    async with Client(build_adapter("claude-code", "/w/api", wait=0)) as c:
        r = await c.call_tool("inbox", {})
    assert "error" in r.data
    assert "POST /mcp auth=yes" in hits and target_hits == []
