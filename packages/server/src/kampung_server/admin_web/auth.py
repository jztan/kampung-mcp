"""Admin sign-in: one token, in-memory sessions, CSRF tokens, a login limiter.

Sessions live in this process only, so a restart signs the owner out; that
keeps session secrets out of SQLite.
"""

import hmac
import math
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

SESSION_TTL = timedelta(hours=12)
LOGIN_WINDOW = timedelta(minutes=15)
LOGIN_FAILURES = 5


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
        self._failures: dict[str, list[datetime]] = {}
        self._lock = threading.Lock()

    def login(self, client: str, token: str) -> AdminSession | None:
        """A new session for the right token, None for a wrong one.

        Raises TooManyAttempts once a client has failed too often, even if
        this attempt has the right token."""
        now = self._clock()
        with self._lock:
            recent = [
                t for t in self._failures.get(client, []) if t > now - LOGIN_WINDOW
            ]
            self._failures[client] = recent
            if len(recent) >= LOGIN_FAILURES:
                wait = recent[0] + LOGIN_WINDOW - now
                raise TooManyAttempts(max(1, math.ceil(wait.total_seconds() / 60)))
            if not hmac.compare_digest(token.encode(), self._token.encode()):
                recent.append(now)
                return None
            session = AdminSession(
                id=secrets.token_urlsafe(32),
                csrf=secrets.token_urlsafe(32),
                expires_at=now + SESSION_TTL,
            )
            self._sessions[session.id] = session
            return session

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
