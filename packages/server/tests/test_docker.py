"""The real image and compose stack, driven through the docker CLI."""

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from docker_testlib import (
    REPO,
    build_image,
    docker_required,
    docker_run,
    parse_key,
    stack,
)

pytestmark = [pytest.mark.docker, docker_required()]
CADDY = "caddy:2.11.7"


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


def test_container_is_hardened(tmp_path: Path) -> None:
    with stack(tmp_path) as s:
        cid = s.compose("ps", "-q", "kampung").stdout.strip()
        host = json.loads(docker_run("inspect", cid).stdout)[0]["HostConfig"]
        assert host["ReadonlyRootfs"] is True
        assert host["CapDrop"] == ["ALL"]
        assert "no-new-privileges:true" in host["SecurityOpt"]
        assert host["LogConfig"]["Config"]["max-size"] == "10m"
        # The database volume is still writable as uid 1000.
        assert s.exec("kampung", "key", "add", "probe").returncode == 0


def test_throttle_keys_on_the_forwarded_client_ip(tmp_path: Path) -> None:
    with stack(tmp_path) as s:
        url = s.url + "/v1/sessions"
        spread = [
            httpx.post(
                url,
                json={},
                headers={
                    "Authorization": f"Bearer kmp_bad{i}",
                    "X-Forwarded-For": f"203.0.113.{i}",
                },
            ).status_code
            for i in range(21)
        ]
        assert set(spread) == {401}  # 21 clients, one wrong key each
        same = [
            httpx.post(url, json={}, headers={"Authorization": f"Bearer kmp_x{i}"})
            for i in range(21)
        ]
        assert same[-1].status_code == 429


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


def test_caddyfile_validates() -> None:
    # A missing bind-mount source would make docker create it as a directory.
    assert (REPO / "deploy" / "Caddyfile").is_file()
    r = docker_run(
        "run",
        "--rm",
        "-e",
        "KAMPUNG_DOMAIN=kampung.example.com",
        "-v",
        f"{REPO}/deploy/Caddyfile:/etc/caddy/Caddyfile:ro",
        CADDY,
        "caddy",
        "validate",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    )
    assert r.returncode == 0, r.stderr


def test_caddyfile_keeps_streaming_and_admin_guard() -> None:
    text = (REPO / "deploy" / "Caddyfile").read_text()
    assert "flush_interval -1" in text
    assert "not remote_ip 127.0.0.1/8 100.64.0.0/10" in text


def test_tls_profile_renders() -> None:
    compose = str(REPO / "docker-compose.yml")
    plain = docker_run("compose", "-f", compose, "config", "--services")
    tls = docker_run(
        "compose", "-f", compose, "--profile", "tls", "config", "--services"
    )
    assert plain.stdout.split() == ["kampung"]
    assert sorted(tls.stdout.split()) == ["caddy", "kampung"]


def test_caddy_admin_api_is_off() -> None:
    # Caddy runs on the host network: an open admin API on localhost:2019
    # would let any local process rewrite the proxy and drop the /admin rule.
    assert (REPO / "deploy" / "Caddyfile").is_file()
    r = docker_run(
        "run",
        "--rm",
        "-e",
        "KAMPUNG_DOMAIN=kampung.example.com",
        "-v",
        f"{REPO}/deploy/Caddyfile:/etc/caddy/Caddyfile:ro",
        CADDY,
        "caddy",
        "adapt",
        "--config",
        "/etc/caddy/Caddyfile",
        "--adapter",
        "caddyfile",
    )
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout).get("admin") == {"disabled": True}


def test_caddy_without_domain_says_so() -> None:
    compose = str(REPO / "docker-compose.yml")
    env = {k: v for k, v in os.environ.items() if k != "KAMPUNG_DOMAIN"}
    base = ["docker", "compose", "-p", f"kampungtest{uuid.uuid4().hex[:8]}"]
    base += ["-f", compose, "--profile", "tls"]
    try:
        r = subprocess.run(
            base + ["run", "--rm", "--no-deps", "caddy"],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
    finally:  # `run --rm` keeps the project's network and volumes
        subprocess.run(base + ["down", "-v"], capture_output=True, env=env)
    assert r.returncode != 0
    assert "KAMPUNG_DOMAIN" in r.stderr + r.stdout


def test_caddy_gets_only_its_settings() -> None:
    compose = str(REPO / "docker-compose.yml")
    env = {**os.environ, "KAMPUNG_HOST_PORT": "9123", "KAMPUNG_DOMAIN": "k.example"}
    r = subprocess.run(
        ["docker", "compose", "-f", compose, "--profile", "tls"]
        + ["config", "--format", "json"],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    caddy = json.loads(r.stdout)["services"]["caddy"]
    # Same port as the publish (shell or .env), and no other .env secrets.
    assert caddy["environment"] == {
        "KAMPUNG_DOMAIN": "k.example",
        "KAMPUNG_HOST_PORT": "9123",
    }


def test_admin_dashboard_in_container(tmp_path: Path) -> None:
    # The templates and stylesheet are package data: this proves they are in
    # the wheel and so in the image.
    with stack(tmp_path, {"KAMPUNG_ADMIN_TOKEN": "t" * 32}) as s:
        login = httpx.get(s.url + "/admin/login")
        assert login.status_code == 200 and "Sign in" in login.text
        css = httpx.get(s.url + "/admin/static/admin.css")
        assert css.status_code == 200 and "--accent" in css.text
