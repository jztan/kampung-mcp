"""A version number that goes up whenever mail or sessions change, so open
event streams know to recompute their counts. The server is one process, so
one in-memory counter covers every stream."""

import threading
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class MailSignal:
    def __init__(self) -> None:
        self._version = 0
        self._lock = threading.Lock()

    def bump(self) -> None:
        with self._lock:
            self._version += 1

    def current(self) -> int:
        return self._version


SIGNAL = MailSignal()


def bumps(fn: Callable[P, R]) -> Callable[P, R]:
    """Bump the signal after fn returns: its transaction has committed, so a
    stream that recomputes sees the change."""

    @wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        result = fn(*args, **kwargs)
        SIGNAL.bump()
        return result

    return wrapper
