"""The real image and compose stack, driven through the docker CLI."""

import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from docker_testlib import (
    build_image,
    docker_required,
    docker_run,
    parse_key,
    stack,
)

pytestmark = [pytest.mark.docker, docker_required()]


def test_image_runs_as_uid_1000() -> None:
    out = docker_run("run", "--rm", build_image(), "id", "-u").stdout
    assert out.strip() == "1000"


def test_data_dir_owned_by_app_user() -> None:
    out = docker_run("run", "--rm", build_image(), "stat", "-c", "%u", "/data").stdout
    assert out.strip() == "1000"


def test_image_holds_no_env_or_tests() -> None:
    out = docker_run(
        "run",
        "--rm",
        build_image(),
        "sh",
        "-c",
        "ls -a /app 2>/dev/null; "
        "find / -name .env -not -path '/proc/*' 2>/dev/null; "
        "find / -path '*kampung*' -name 'test_*.py' -not -path '/proc/*' 2>/dev/null",
    ).stdout
    assert ".env" not in out and "test_" not in out


def test_cli_works_in_image() -> None:
    r = docker_run("run", "--rm", build_image(), "kampung", "key", "list")
    assert r.returncode == 0, r.stderr


def test_fresh_server_is_closed(tmp_path: Path) -> None:
    with stack(tmp_path) as s:
        assert httpx.get(s.url + "/health").json() == {"version": "0.1.0"}
        assert httpx.post(s.url + "/mcp", json={}).status_code == 401
        assert httpx.get(s.url + "/v1/sessions/1/peek").status_code == 401


def test_published_on_loopback_only(tmp_path: Path) -> None:
    with stack(tmp_path) as s:
        assert s.compose("port", "kampung", "8000").stdout.startswith("127.0.0.1:")


async def test_first_run_flow(tmp_path: Path) -> None:
    with stack(tmp_path) as s:
        key = parse_key(s.exec("kampung", "key", "add", "laptop").stdout)
        assert s.exec("kampung", "key", "add", "desktop").returncode == 0
        assert s.exec("kampung", "pair", "laptop", "desktop").returncode == 0
        auth = {"Authorization": f"Bearer {key}"}
        reg = httpx.post(
            s.url + "/v1/sessions",
            headers=auth,
            json={
                "agent": "claude-code",
                "cwd": "/w/api",
                "repo": "api",
                "hooks": True,
            },
        )
        assert reg.status_code == 200, reg.text
        headers = {**auth, "x-kampung-session": str(reg.json()["session"])}
        transport = StreamableHttpTransport(s.url + "/mcp", headers=headers)
        async with Client(transport) as c:
            peers = (await c.call_tool("list_peers", {})).data
        assert "error" not in peers  # served from the named-volume DB


def test_data_survives_recreate(tmp_path: Path) -> None:
    with stack(tmp_path) as s:
        assert s.exec("kampung", "key", "add", "laptop").returncode == 0
        t = time.monotonic()
        assert s.compose("stop").returncode == 0
        assert time.monotonic() - t < 8
        assert s.compose("down").returncode == 0  # no -v: the volume stays
        assert s.compose("up", "-d", "--wait").returncode == 0
        assert "laptop" in s.exec("kampung", "key", "list").stdout


def test_env_file_cannot_move_port_or_db(tmp_path: Path) -> None:
    env = {
        "KAMPUNG_HTTP_PORT": "9999",
        "KAMPUNG_HTTP_HOST": "127.0.0.1",
        "KAMPUNG_DB_PATH": "/tmp/elsewhere.db",
        "KAMPUNG_ENV_PROBE": "seen",
    }
    with stack(tmp_path, env) as s:
        # The env_file did reach the container; only the pinned values lost.
        assert s.exec("printenv", "KAMPUNG_ENV_PROBE").stdout.strip() == "seen"
        assert s.exec("printenv", "KAMPUNG_HTTP_PORT").stdout.strip() == "8000"
        assert httpx.get(s.url + "/health").status_code == 200
        assert s.exec("kampung", "key", "add", "laptop").returncode == 0
        assert s.exec("test", "-s", "/data/kampung.db").returncode == 0
