"""Admin sign-in: one token, in-memory sessions, CSRF tokens, a login limiter.

Sessions live in this process only, so a restart signs the owner out; that
keeps session secrets out of SQLite.
"""

import hmac
import math
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

SESSION_TTL = timedelta(hours=12)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_FAILURES = 5
MAX_CLIENTS = 10_000


@dataclass
class AdminSession:
    id: str
    csrf: str
    expires_at: datetime
    flash: tuple[str, str] | None = None
    new_key: tuple[str, str] | None = None  # (machine, plaintext key), shown once


class TooManyAttempts(Exception):
    def __init__(self, minutes: int) -> None:
        super().__init__(f"try again in {minutes} minutes")
        self.minutes = minutes


class AdminAuth:
    def __init__(self, token: str, clock: Callable[[], datetime]) -> None:
        self._token = token
        self._clock = clock
        self._sessions: dict[str, AdminSession] = {}
        # Least recently failed first, so idle clients are dropped from the front.
        self._failures: OrderedDict[str, list[datetime]] = OrderedDict()
        self._lock = threading.Lock()

    def device_proof(self) -> str:
        """What a browser keeps after signing in, to pass the limit next time.
        Derived from the token, so changing the token forgets every browser."""
        return hmac.new(self._token.encode(), b"admin-device", "sha256").hexdigest()

    def login(
        self, client: str, token: str, device: str | None = None
    ) -> AdminSession | None:
        """A new session for the right token, None for a wrong one.

        Raises TooManyAttempts once a client has failed too often, even if
        this attempt has the right token. A browser that shows `device_proof`
        is neither limited nor counted: a flood from the owner's address must
        not lock out the owner, and it still needs the token."""
        now = self._clock()
        known = device is not None and hmac.compare_digest(
            device.encode(), self.device_proof().encode()
        )
        with self._lock:
            self._drop_idle(now)
            recent = [
                t for t in self._failures.get(client, []) if t > now - LOGIN_WINDOW
            ]
            if not known and len(recent) >= LOGIN_FAILURES:
                wait = recent[0] + LOGIN_WINDOW - now
                raise TooManyAttempts(max(1, math.ceil(wait.total_seconds() / 60)))
            if not hmac.compare_digest(token.encode(), self._token.encode()):
                if not known:
                    self._failures[client] = [*recent, now]
                    self._failures.move_to_end(client)
                    while len(self._failures) > MAX_CLIENTS:
                        self._failures.popitem(last=False)
                return None
            session = AdminSession(
                id=secrets.token_urlsafe(32),
                csrf=secrets.token_urlsafe(32),
                expires_at=now + SESSION_TTL,
            )
            self._sessions[session.id] = session
            return session

    def tracked(self) -> int:
        return len(self._failures)

    def _drop_idle(self, now: datetime) -> None:
        for client, times in list(self._failures.items()):
            if times[-1] > now - LOGIN_WINDOW:
                break
            del self._failures[client]

    def get(self, session_id: str | None) -> AdminSession | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.expires_at <= self._clock():
                del self._sessions[session_id]
                return None
            return session

    def logout(self, session: AdminSession) -> None:
        with self._lock:
            self._sessions.pop(session.id, None)

    @staticmethod
    def csrf_ok(session: AdminSession, sent: object) -> bool:
        return isinstance(sent, str) and hmac.compare_digest(
            sent.encode(), session.csrf.encode()
        )
