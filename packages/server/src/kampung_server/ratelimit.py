"""In-memory, per-process send limiter keyed by machine."""

import threading
from collections import defaultdict, deque
from datetime import datetime, timedelta

from .errors import KampungError
from .limits import SEND_RATE, SEND_RATE_WINDOW


class RateLimiter:
    def __init__(
        self, limit: int = SEND_RATE, window: timedelta = SEND_RATE_WINDOW
    ) -> None:
        self._limit = limit
        self._window = window
        self._sent: dict[int, deque[datetime]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, machine_id: int, now: datetime) -> None:
        with self._lock:
            sent = self._sent[machine_id]
            while sent and sent[0] <= now - self._window:
                sent.popleft()
            if len(sent) >= self._limit:
                raise KampungError(
                    "rate_limited", "Too many sends; try again in a minute."
                )
            sent.append(now)
