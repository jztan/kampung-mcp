"""Machine and repo names, session names, and mailbox addresses (machine/repo,
machine/repo#xx or machine/repo@name)."""

import re
import unicodedata
from dataclasses import dataclass

from .errors import KampungError
from .limits import MAX_SESSION_NAME_CHARS

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SUFFIX = re.compile(r"[0-9a-f]{2,4}")
# A session name: lower-case words of letters and digits joined by '-'.
SESSION_NAME = re.compile(
    rf"(?=.{{1,{MAX_SESSION_NAME_CHARS}}}$)[a-z0-9]+(-[a-z0-9]+)*"
)
_ADDRESS_HINT = (
    "Use machine/repo, machine/repo#xx or machine/repo@name; call list_peers to "
    "see addresses."
)


def validate_name(kind: str, value: str) -> str:
    if not _NAME.fullmatch(value):
        raise KampungError(
            "invalid_name",
            f"A {kind} name is 1 to 64 letters, digits, '.', '_' or '-', "
            "starting with a letter or digit.",
        )
    return value


@dataclass(frozen=True)
class Address:
    machine: str
    repo: str
    suffix: str | None
    name: str | None = None

    def __str__(self) -> str:
        base = f"{self.machine}/{self.repo}"
        if self.suffix:
            return f"{base}#{self.suffix}"
        return f"{base}@{self.name}" if self.name else base


def slug_name(text: str, limit: int = MAX_SESSION_NAME_CHARS) -> str | None:
    """A session name from a title: accents dropped, lower case, each run of
    anything but a letter or digit made one '-', cut to `limit` characters at
    a word boundary when there is one. None when nothing is left."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")
    if len(slug) > limit:
        cut = slug[: limit + 1]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else slug[:limit]
    return slug or None


def parse_address(value: str) -> Address:
    path, at, name = value.partition("@")
    path, sep, suffix = path.partition("#")
    parts = path.split("/")
    if (
        len(parts) != 2
        or not all(_NAME.fullmatch(p) for p in parts)
        or (sep and not _SUFFIX.fullmatch(suffix))
        or (at and (sep or not SESSION_NAME.fullmatch(name.lower())))
    ):
        raise KampungError("invalid_address", _ADDRESS_HINT)
    return Address(
        parts[0], parts[1], suffix if sep else None, name.lower() if at else None
    )
