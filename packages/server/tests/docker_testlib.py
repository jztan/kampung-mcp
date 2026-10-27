"""Helpers for the opt-in Docker tests (KAMPUNG_DOCKER_TESTS=1)."""

import functools
import json
import os
import shutil
import socket
import subprocess
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
TAG = "kampung-mcp:test"


def docker_required() -> pytest.MarkDecorator:
    """Skip unless asked for, docker exists, and no live .env could leak in."""
    if os.environ.get("KAMPUNG_DOCKER_TESTS") != "1":
        return pytest.mark.skip(reason="set KAMPUNG_DOCKER_TESTS=1 to run")
    if shutil.which("docker") is None:
        return pytest.mark.skip(reason="docker not found")
    if (REPO / ".env").exists():
        return pytest.mark.skip(reason="a live .env is in the repo root")
    return pytest.mark.docker


def docker_run(*args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check, timeout=600
    )


@functools.cache
def build_image() -> str:
    docker_run("build", "-q", "-t", TAG, str(REPO), check=True)
    return TAG


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def parse_key(out: str) -> str:
    return out.split("Key (shown once): ", 1)[1].strip()


@dataclass
class Stack:
    """One running copy of the real docker-compose.yml, on its own project."""

    project: str
    files: list[str]
    port: int

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        cmd = ["docker", "compose", "-p", self.project]
        for f in self.files:
            cmd += ["-f", f]
        env = {**os.environ, "KAMPUNG_HOST_PORT": str(self.port)}
        return subprocess.run(
            cmd + list(args), capture_output=True, text=True, env=env, timeout=300
        )

    def exec(self, *cmd: str) -> subprocess.CompletedProcess[str]:
        return self.compose("exec", "-T", "kampung", *cmd)


@contextmanager
def stack(tmp: Path, env: dict[str, str] | None = None) -> Iterator[Stack]:
    """Run the real compose file plus an override that swaps in the test image
    and, if given, an extra env_file. Rendering with `docker compose config`
    would inline env_file and drop it, so the real file is used as is."""
    service: dict[str, object] = {"image": build_image(), "pull_policy": "never"}
    if env is not None:
        (tmp / ".env").write_text("".join(f"{k}={v}\n" for k, v in env.items()))
        service["env_file"] = [{"path": str(tmp / ".env")}]
    override = tmp / "override.json"  # JSON is valid YAML
    override.write_text(json.dumps({"services": {"kampung": service}}))
    s = Stack(
        f"kampungtest{uuid.uuid4().hex[:8]}",
        [str(REPO / "docker-compose.yml"), str(override)],
        free_port(),
    )
    try:
        up = s.compose("up", "-d", "--wait")
        assert up.returncode == 0, up.stderr
        yield s
    finally:
        s.compose("down", "-v")
