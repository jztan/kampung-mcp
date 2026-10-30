"""Machine and repo names, and mailbox addresses (machine/repo or machine/repo#xx)."""

import re
from dataclasses import dataclass

from .errors import KampungError

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_SUFFIX = re.compile(r"[0-9a-f]{2,4}")
_ADDRESS_HINT = "Use machine/repo or machine/repo#xx; call list_peers to see addresses."


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

    def __str__(self) -> str:
        base = f"{self.machine}/{self.repo}"
        return f"{base}#{self.suffix}" if self.suffix else base


def parse_address(value: str) -> Address:
    path, sep, suffix = value.partition("#")
    parts = path.split("/")
    if (
        len(parts) != 2
        or not all(_NAME.fullmatch(p) for p in parts)
        or (sep and not _SUFFIX.fullmatch(suffix))
    ):
        raise KampungError("invalid_address", _ADDRESS_HINT)
    return Address(parts[0], parts[1], suffix if sep else None)
