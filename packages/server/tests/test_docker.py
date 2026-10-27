"""The real image and compose stack, driven through the docker CLI."""

import pytest

from docker_testlib import build_image, docker_required, docker_run

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
