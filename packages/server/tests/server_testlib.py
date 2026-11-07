"""Helpers shared by server tests.

A uniquely named module, because `from conftest import` breaks once a second
package's conftest.py is in the same pytest run.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from kampung_server.models import Session


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 27, 9, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


MakeSession = Callable[..., Session]
