import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from client_testlib import git_repo
from kampung_client import outbox
from kampung_client.outbox import (
    OutboxError,
    delete_if_same,
    ensure_excluded,
    outbox_dir,
    read_file,
    valid_name,
)

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file types")

# The same vectors the server tests use, so the two name rules cannot drift.
GOOD_NAMES = ["README.md", "a", "shot-1.png", "notes_v2.txt", "x" * 100]
BAD_NAMES = [
    "../x",
    "a/b",
    "a\\b",
    "D:x",
    "a.txt:stream",
    "CON",
    "con.txt",
    "Com1.log",
    "CONIN$",
    "a.",
    ".hidden",
    "x" * 101,
    "",
    "a b",
    "na‮me",
    "line\nbreak",
]


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_good_names(name: str) -> None:
    assert valid_name(name)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_bad_names_are_refused(name: str, tmp_path: Path) -> None:
    assert not valid_name(name)
    box = tmp_path / "kampung-outbox"
    box.mkdir()
    with pytest.raises(OutboxError) as e:
        read_file(box, name, 1000)
    assert e.value.code == "invalid_attachment"


def _box(tmp_path: Path) -> Path:
    box = tmp_path / "kampung-outbox"
    box.mkdir()
    return box


def test_outbox_is_at_the_git_top(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, "api")
    (repo / "src").mkdir()
    assert outbox_dir(str(repo / "src")) == repo / "kampung-outbox"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert outbox_dir(str(plain)) == plain / "kampung-outbox"


def test_reads_a_plain_file(tmp_path: Path) -> None:
    box = _box(tmp_path)
    (box / "a.md").write_bytes(b"hello")
    f = read_file(box, "a.md", 1000)
    assert (f.name, f.data) == ("a.md", b"hello")


def test_missing_file_names_the_full_path(tmp_path: Path) -> None:
    box = _box(tmp_path)
    with pytest.raises(OutboxError) as e:
        read_file(box, "nope.md", 1000)
    assert e.value.code == "attachment_not_found" and str(box) in e.value.hint


def test_missing_outbox_names_the_full_path(tmp_path: Path) -> None:
    with pytest.raises(OutboxError) as e:
        read_file(tmp_path / "kampung-outbox", "a.md", 1000)
    assert e.value.code == "attachment_not_found"
    assert str(tmp_path / "kampung-outbox") in e.value.hint


@posix_only
def test_outbox_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "secrets"
    real.mkdir()
    (real / "id_ed25519").write_bytes(b"key")
    (tmp_path / "kampung-outbox").symlink_to(real)
    with pytest.raises(OutboxError) as e:
        read_file(tmp_path / "kampung-outbox", "id_ed25519", 1000)
    assert e.value.code == "invalid_attachment"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")
def test_outbox_that_is_a_junction_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "secrets"
    real.mkdir()
    (real / "a.md").write_bytes(b"key")
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(tmp_path / "kampung-outbox"), str(real)],
        check=True,
        capture_output=True,
    )
    with pytest.raises(OutboxError):
        read_file(tmp_path / "kampung-outbox", "a.md", 1000)


@posix_only
def test_file_that_is_a_symlink_is_refused(tmp_path: Path) -> None:
    box = _box(tmp_path)
    secret = tmp_path / "id_ed25519"
    secret.write_bytes(b"key")
    (box / "a.md").symlink_to(secret)
    with pytest.raises(OutboxError):
        read_file(box, "a.md", 1000)


def test_hard_linked_file_is_refused(tmp_path: Path) -> None:
    box = _box(tmp_path)
    secret = tmp_path / "id_ed25519"
    secret.write_bytes(b"key")
    os.link(secret, box / "a.md")
    with pytest.raises(OutboxError):
        read_file(box, "a.md", 1000)


@posix_only
def test_fifo_does_not_hang(tmp_path: Path) -> None:
    box = _box(tmp_path)
    os.mkfifo(box / "a.md")
    start = time.monotonic()
    with pytest.raises(OutboxError):
        read_file(box, "a.md", 1000)
    assert time.monotonic() - start < 1


def test_directory_is_refused(tmp_path: Path) -> None:
    box = _box(tmp_path)
    (box / "sub").mkdir()
    with pytest.raises(OutboxError):
        read_file(box, "sub", 1000)


def test_file_over_the_cap_is_refused(tmp_path: Path) -> None:
    box = _box(tmp_path)
    (box / "a.md").write_bytes(b"x" * 11)
    with pytest.raises(OutboxError) as e:
        read_file(box, "a.md", 10)
    assert e.value.code == "attachment_too_large"


def test_delete_removes_only_that_file(tmp_path: Path) -> None:
    box = _box(tmp_path)
    (box / "a.md").write_bytes(b"a")
    (box / "b.md").write_bytes(b"b")
    delete_if_same(box, read_file(box, "a.md", 10))
    assert sorted(p.name for p in box.iterdir()) == ["b.md"]


def test_delete_skips_a_file_replaced_since_reading(tmp_path: Path) -> None:
    box = _box(tmp_path)
    (box / "a.md").write_bytes(b"old")
    f = read_file(box, "a.md", 10)
    (box / "a.md").unlink()
    (box / "keep.md").write_bytes(b"new")  # take the freed inode first
    (box / "a.md").write_bytes(b"new")
    delete_if_same(box, f)
    assert (box / "a.md").read_bytes() == b"new"


def test_exclude_line_added_once(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, "api")
    for _ in range(2):
        ensure_excluded(repo / "kampung-outbox")
    text = (repo / ".git" / "info" / "exclude").read_text()
    assert text.count("/kampung-outbox/") == 1
    (repo / "kampung-outbox").mkdir()
    (repo / "kampung-outbox" / "a.md").write_text("x")
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    assert "kampung-outbox" not in status


def test_exclude_works_in_a_linked_worktree(tmp_path: Path) -> None:
    repo = git_repo(tmp_path, "api")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=a",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
        check=True,
    )
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", str(linked)], check=True
    )
    ensure_excluded(linked / "kampung-outbox")
    (linked / "kampung-outbox").mkdir()
    (linked / "kampung-outbox" / "a.md").write_text("x")
    status = subprocess.run(
        ["git", "-C", str(linked), "status", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    assert "kampung-outbox" not in status


def test_exclude_never_creates_git_outside_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    ensure_excluded(plain / "kampung-outbox")
    assert not (plain / ".git").exists()


def test_git_runs_with_fsmonitor_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(args: list[str], *a: Any, **kw: Any) -> Any:
        calls.append(list(args))
        return real(args, *a, **kw)

    repo = git_repo(tmp_path, "api")
    monkeypatch.setattr(outbox.subprocess, "run", spy)
    outbox_dir(str(repo))
    ensure_excluded(repo / "kampung-outbox")
    assert calls and all("core.fsmonitor=" in c for c in calls)
