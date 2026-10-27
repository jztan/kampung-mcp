"""The per-session stdio MCP adapter: finds its Kampung session and forwards
the three tools to the server with that identity attached."""

from __future__ import annotations

import os
import time

from . import hook
from .hook import Ancestor


def _distance(chain: list[Ancestor], ancestors: object) -> int | None:
    if not isinstance(ancestors, list):
        return None
    recorded = {tuple(a) for a in ancestors if isinstance(a, list) and len(a) == 2}
    for i, link in enumerate(chain):
        if link in recorded:
            return i
    return None


def pick_session(
    chain: list[Ancestor], cwd: str, files: list[dict[str, object]]
) -> dict[str, object] | None:
    """The hook-written session file whose ancestors meet `chain` nearest.

    Ties prefer a matching cwd, then the newest file. Files written by an
    adapter (hooks false) are never adopted: their chain is the adapter's own,
    so they would always win.
    """
    best: tuple[int, int, float] | None = None
    choice = None
    for f in files:
        if f.get("hooks") is not True:
            continue
        d = _distance(chain, f.get("ancestors"))
        if d is None:
            continue
        written = f.get("written_at")
        key = (
            d,
            0 if f.get("cwd") == cwd else 1,
            -float(written) if isinstance(written, (int, float)) else 0.0,
        )
        if best is None or key < best:
            best, choice = key, f
    return choice


class SessionResolver:
    """Lazily finds this adapter's session; never revives an ended one."""

    def __init__(
        self, agent: str, cwd: str, wait: float = 2.0, poll: float = 0.1
    ) -> None:
        self.agent = agent
        self.cwd = cwd
        self.wait = wait
        self.poll = poll
        self._chain: list[Ancestor] | None = None
        self._data: dict[str, object] | None = None

    def _ancestors(self) -> list[Ancestor]:
        if self._chain is None:
            self._chain = hook.ancestors()
        return self._chain

    def _pick(self) -> dict[str, object] | None:
        return pick_session(self._ancestors(), self.cwd, hook.list_session_files())

    def _self_register(self) -> dict[str, object]:
        data = hook.register(self.agent, self.cwd, False)
        hook.write_session_file(f"adapter-{os.getpid()}", data)
        return data

    def current(self) -> dict[str, object]:
        if self._data is None:
            end = time.monotonic() + self.wait
            found = self._pick()
            while found is None and time.monotonic() < end:
                time.sleep(self.poll)
                found = self._pick()
            self._data = found if found is not None else self._self_register()
        return self._data

    def refresh(self) -> dict[str, object]:
        """After `session_ended`: adopt a newer hook session, or start a fresh one."""
        old = self._data.get("session") if self._data else None
        found = self._pick()
        if found is not None and found.get("session") != old:
            self._data = found
        else:
            self._data = self._self_register()
        return self._data
