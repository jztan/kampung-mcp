"""The outbox: the one folder an attachment may come from.

A session saves the file into `kampung-outbox/` at its project root and
names it in `send`. The adapter never reads a path the model chose, and it
treats the folder as hostile: a tricked session could swap it for a link
to `~/.ssh`. So the folder must be a real directory, the file a regular
file with one link, opened without following links, and only that file is
deleted after the server accepts it, and only if it is still the same file.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

OUTBOX_NAME = "kampung-outbox"
EXCLUDE_LINE = f"/{OUTBOX_NAME}/"
_GIT = ["git", "-c", "core.fsmonitor="]  # a tricked .git/config runs nothing
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_DEVICE = re.compile(r"(?i)(?:con|prn|aux|nul|com[0-9]|lpt[0-9]|conin\$|conout\$)")


class OutboxError(Exception):
    def __init__(self, code: str, hint: str) -> None:
        super().__init__(f"{code}: {hint}")
        self.code = code
        self.hint = hint


@dataclass(frozen=True)
class OutboxFile:
    name: str
    data: bytes
    dev: int
    ino: int


def valid_name(name: str) -> bool:
    """The server's name rule, kept in step by shared test vectors."""
    if not _NAME.fullmatch(name) or name.endswith("."):
        return False
    return not _DEVICE.fullmatch(name.split(".", 1)[0])


def _git(cwd: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(
            [*_GIT, "-C", str(cwd), *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def outbox_dir(cwd: str) -> Path:
    """`kampung-outbox` at the git worktree top, or in `cwd` outside git."""
    top = _git(Path(cwd), "rev-parse", "--show-toplevel")
    return Path(top or cwd) / OUTBOX_NAME


def _bad(hint: str) -> OutboxError:
    return OutboxError("invalid_attachment", hint)


def _check_dir(outbox: Path) -> None:
    try:
        st = os.lstat(outbox)
    except FileNotFoundError as e:
        raise OutboxError(
            "attachment_not_found",
            f"Save the file in {outbox} first; that folder does not exist yet.",
        ) from e
    if not stat.S_ISDIR(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE:
        raise _bad(f"{outbox} must be a plain folder, not a link.")


def read_file(outbox: Path, name: str, cap: int) -> OutboxFile:
    """Read `name` from the outbox, refusing anything that is not a plain
    file directly inside a plain folder."""
    if not valid_name(name):
        raise _bad(
            "Name a file directly in the outbox: letters, digits, '.', '_' or "
            "'-', starting with a letter or digit."
        )
    _check_dir(outbox)
    if sys.platform == "win32":
        return _read_windows(outbox, name, cap)
    return _read_posix(outbox, name, cap)


def _missing(outbox: Path, name: str) -> OutboxError:
    return OutboxError("attachment_not_found", f"No file {name} in {outbox}.")


def _check_file(st: os.stat_result, name: str, cap: int) -> None:
    if not stat.S_ISREG(st.st_mode) or getattr(st, "st_file_attributes", 0) & _REPARSE:
        raise _bad(f"{name} must be a plain file, not a link or folder.")
    if st.st_nlink != 1:
        raise _bad(f"{name} must not be a hard link to another file.")
    if st.st_size > cap:
        raise OutboxError(
            "attachment_too_large", f"{name} is {st.st_size} bytes; the limit is {cap}."
        )


def _read_all(fd: int, cap: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while size <= cap:
        chunk = os.read(fd, min(65536, cap + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _open_dir(outbox: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        return os.open(outbox, flags)
    except OSError as e:
        raise _bad(f"{outbox} must be a plain folder, not a link.") from e


def _read_posix(outbox: Path, name: str, cap: int) -> OutboxFile:
    dir_fd = _open_dir(outbox)
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            fd = os.open(name, flags, dir_fd=dir_fd)
        except FileNotFoundError as e:
            raise _missing(outbox, name) from e
        except OSError as e:  # a link (ELOOP) or something unopenable
            raise _bad(f"{name} must be a plain file, not a link.") from e
        try:
            st = os.fstat(fd)
            _check_file(st, name, cap)
            data = _read_all(fd, cap)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)
    if len(data) > cap:
        raise OutboxError("attachment_too_large", f"{name} grew past {cap} bytes.")
    return OutboxFile(name, data, st.st_dev, st.st_ino)


def _read_windows(outbox: Path, name: str, cap: int) -> OutboxFile:
    path = outbox / name
    try:
        before = os.lstat(path)
    except FileNotFoundError as e:
        raise _missing(outbox, name) from e
    _check_file(before, name, cap)
    if os.path.dirname(os.path.realpath(path)) != os.path.realpath(outbox):
        raise _bad(f"{name} is not directly in {outbox}.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != (before.st_dev, before.st_ino):
            raise _bad(f"{name} changed while it was being read.")
        _check_file(st, name, cap)
        data = _read_all(fd, cap)
    finally:
        os.close(fd)
    if len(data) > cap:
        raise OutboxError("attachment_too_large", f"{name} grew past {cap} bytes.")
    return OutboxFile(name, data, st.st_dev, st.st_ino)


def delete_if_same(outbox: Path, f: OutboxFile) -> None:
    """Delete the sent file, only if it is still the file that was read.
    Never anything else in the folder, and never through a link."""
    try:
        _check_dir(outbox)
        if sys.platform == "win32":
            st = os.lstat(outbox / f.name)
            if (st.st_dev, st.st_ino) == (f.dev, f.ino) and stat.S_ISREG(st.st_mode):
                os.unlink(outbox / f.name)
            return
        dir_fd = _open_dir(outbox)
        try:
            st = os.stat(f.name, dir_fd=dir_fd, follow_symlinks=False)
            if (st.st_dev, st.st_ino) == (f.dev, f.ino) and stat.S_ISREG(st.st_mode):
                os.unlink(f.name, dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
    except (OSError, OutboxError):
        pass  # the file stays; the send itself already succeeded


def ensure_excluded(outbox: Path) -> None:
    """Keep the outbox out of git through the repo's local exclude file, so
    no tracked file changes. Outside a repo, do nothing."""
    top = outbox.parent
    found = _git(top, "rev-parse", "--show-toplevel")
    if found is None or Path(found).resolve() != top.resolve():
        return
    rel = _git(top, "rev-parse", "--git-path", "info/exclude")
    if rel is None:
        return
    exclude = (top / rel).resolve()
    try:
        text = exclude.read_text() if exclude.exists() else ""
        if EXCLUDE_LINE in text.splitlines():
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        prefix = "" if text == "" or text.endswith("\n") else "\n"
        with exclude.open("a") as out:
            out.write(f"{prefix}{EXCLUDE_LINE}\n")
    except OSError:
        pass  # the outbox still works; git may just list it
