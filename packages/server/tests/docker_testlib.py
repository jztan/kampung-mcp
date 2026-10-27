"""Helpers for the opt-in Docker tests (KAMPUNG_DOCKER_TESTS=1)."""

import functools
import os
import shutil
import subprocess
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
